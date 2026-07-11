#!/usr/bin/env python3
"""
eval_signals.py — Golden-dataset evaluation harness for signal extraction.

WHY THIS EXISTS
    The pytest suite (tests/) verifies the *code* (scoring, tiering, exclusions)
    and is deterministic by rule: no API calls, no HTTP. It cannot tell you when a
    prompt tweak or a vendor model update makes the LLM start *missing real medical
    signals*. This harness covers that gap: it runs labeled sites through the real
    extractor and measures extraction quality against a known-good baseline.

    It is opt-in and, in --live mode, spends tokens. It is NOT collected by pytest.

GOLDEN FORMAT — one directory per case under evals/golden/<case_id>/:
    labels.json             required. practice fields + labeling metadata + expected states:
                              { "practice_name": "...", "website_url": "...",
                                "specialty": "OBGYN", "address_city": "...", "address_state": "...",
                                "reviewed": true,
                                "rubric_version": "femaseed-rubric-v1",
                                "page_sha256": "<page_fingerprint of page.txt>",
                                "anchors": { "cash_pay_signal": "verbatim on-page quote", ... },
                                "expected": { "cash_pay_signal": "yes", "ivf_listed": "no", ... } }
                            'expected' keys must exactly match the ICP's signal IDs; values are
                            yes / no / not_found. Every yes/no needs its verbatim anchor in
                            'anchors' — preflight verifies each appears in page.txt under
                            normalize_anchor_text. page_sha256 pins the text the labels were
                            authored against (page_fingerprint recomputes it).
    page.txt                the captured website text (nonempty). Required for --live and for
                              the anchor checks. (Tip: copy a real Evidence Vault snapshot.)
    recorded_response.json  optional. A saved model signals list, replayed by --offline:
                              { "signals": [ { "signal_id": "...", "signal_state": "yes",
                                              "confidence": "high", "evidence_text": "...",
                                              "source_url": "..." }, ... ] }

PREFLIGHT
    Gating modes (--live / --check / --update-baseline) validate the dataset BEFORE any
    extractor call: production requires exactly 20 reviewed cases, full ICP key coverage,
    >= 4 labeled-yes per signal, one rubric_version, matching page_sha256 fingerprints, and
    anchored yes/no labels. --dev-dataset relaxes everything but the schema checks (for the
    shipped synthetic demos); it is never valid with --update-baseline.

MODES
    --offline   replay recorded_response.json through the REAL validator. No API,
                fully deterministic. Use it in CI and to self-test the harness.
    --live      call the real extractor (Claude) on page.txt. Spends tokens.
    --check     enforce the thresholds in evals/baseline.json; exit 1 on regression
                or when any discovered case is unreviewed.
    --update-baseline   write current metrics into the baseline file. Refuses when
                any discovered case is unreviewed, and refuses offline mode — a
                production baseline must come from a live extractor run.

REVIEW STATUS IS AUTHORITATIVE
    Only cases with "reviewed": true in labels.json contribute to metrics. Drafts
    (scaffolded or hand-started) are excluded from every aggregate, never spend
    live tokens, and are reported as excluded-unreviewed. An unreviewed case
    fails --check even when it cannot be executed (e.g. no recorded_response.json).
    An empty reviewed set is a hard failure, not an n/a report.

METRICS
    state_accuracy   share of (case, signal) pairs whose signal_state matches the label.
    yes_recall       of signals labeled "yes", how many the model confirmed "yes".
                       This is the business-critical number: a miss is a lost target.
    yes_precision    of signals the model called "yes", how many were truly "yes".
    anchor_rate      of model "yes" signals, how many quote text that appears verbatim
                       in page.txt (catches fabricated evidence).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from enrichment.signal_extractor import _validate_and_clean_signals

_REPO = Path(__file__).resolve().parent
_DEFAULT_ICP = _REPO / "config" / "clients" / "obgyn_femasys" / "icp_checklist.json"
_DEFAULT_CONFIG = _REPO / "config" / "clients" / "obgyn_femasys" / "run_config.json"
_DEFAULT_GOLDEN = _REPO / "evals" / "golden"
_DEFAULT_BASELINE = _REPO / "evals" / "baseline.json"


def load_icp_signals(path: Path) -> list[dict]:
    """Return the ICP signal definitions from a cartridge JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))["signals"]


def load_run_meta(path: Path) -> tuple[str, str]:
    """Return (target_specialty, contact_strategy) from a run_config, empty if absent."""
    if not path.exists():
        return "", ""
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return cfg.get("target_specialty", ""), cfg.get("contact_strategy", "")


def discover_cases(golden_dir: Path) -> list[Path]:
    """Return every golden case directory (one that contains labels.json), sorted."""
    if not golden_dir.is_dir():
        return []
    return sorted(p.parent for p in golden_dir.glob("*/labels.json"))


def _signals_to_state_map(signals: list[dict]) -> dict[str, dict]:
    """Index a normalized signals list by signal_id."""
    return {s["signal_id"]: s for s in signals}


def run_case_offline(case_dir: Path, icp_signals: list[dict]) -> dict:
    """Replay a recorded model response through the real validator (no API)."""
    recorded = json.loads((case_dir / "recorded_response.json").read_text(encoding="utf-8"))
    cleaned = _validate_and_clean_signals(recorded.get("signals", []), icp_signals)
    return _signals_to_state_map(cleaned)


def run_case_live(case_dir: Path, icp_signals: list[dict],
                  target_specialty: str, contact_strategy: str) -> dict:
    """Run the real extractor (Claude) against the case's page.txt. Spends tokens."""
    from enrichment.signal_extractor import extract_signals  # local: avoids API needs in --offline

    labels = json.loads((case_dir / "labels.json").read_text(encoding="utf-8"))
    page_text = (case_dir / "page.txt").read_text(encoding="utf-8")
    record = {
        "practice_name": labels.get("practice_name", case_dir.name),
        "specialty": labels.get("specialty", ""),
        "address_city": labels.get("address_city", ""),
        "address_state": labels.get("address_state", ""),
        "address_zip": labels.get("address_zip", ""),
        "website_url": labels.get("website_url", ""),
    }
    extract_signals(record, icp_signals, page_text, "eval",
                    target_specialty=target_specialty, contact_strategy=contact_strategy)
    return _signals_to_state_map(record.get("signals", []))


def _slugify(name: str, fallback: str) -> str:
    """Reduce a practice name to a filesystem-safe golden case id."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug[:40] or fallback


def scaffold_from_run(run_dir: Path, golden_dir: Path,
                      icp_signals: list[dict]) -> tuple[list[str], list[tuple]]:
    """Generate golden case stubs from a completed run's Evidence Vault.

    For each record with an archived snapshot: write page.txt (rehydrated vault
    text), labels.json (practice fields + expected states PREFILLED from the run's
    own extraction and flagged ``reviewed: false``), and recorded_response.json
    (the run's signals, for --offline replay). Existing case dirs are never
    clobbered. The prefilled expected states are a draft a human must verify, not
    ground truth — the harness refuses to gate on a case until ``reviewed`` is true.
    """
    from output.evidence_writer import read_record_context_text

    et = run_dir / "enriched_targets.json"
    if not et.exists():
        print(f"No enriched_targets.json in {run_dir}", file=sys.stderr)
        return [], []
    data = json.loads(et.read_text(encoding="utf-8"))
    records = data.get("records", data) if isinstance(data, dict) else data
    sig_ids = [s["signal_id"] for s in icp_signals]

    created: list[str] = []
    skipped: list[tuple] = []
    used: set[str] = set()
    for rec in records:
        rid = str(rec.get("id", "")).strip()
        page_text = read_record_context_text(run_dir, rid)
        name = rec.get("practice_name") or rid or "case"
        if not page_text.strip():
            skipped.append((name, "no Evidence Vault snapshot"))
            continue
        base = _slugify(rec.get("practice_name", ""), rid or "case")
        case_id, n = base, 2
        while case_id in used or (golden_dir / case_id).exists():
            case_id, n = f"{base}_{n}", n + 1
            if n > 99:
                break
        if (golden_dir / case_id).exists():
            skipped.append((case_id, "already exists"))
            continue
        used.add(case_id)
        case_dir = golden_dir / case_id
        case_dir.mkdir(parents=True)
        (case_dir / "page.txt").write_text(page_text, encoding="utf-8")
        rec_sigs = {s.get("signal_id"): s for s in rec.get("signals", [])}
        expected = {sid: rec_sigs.get(sid, {}).get("signal_state", "not_found") for sid in sig_ids}
        labels = {
            "practice_name": rec.get("practice_name", ""),
            "website_url": rec.get("website_url", ""),
            "specialty": rec.get("specialty", ""),
            "address_city": rec.get("address_city", ""),
            "address_state": rec.get("address_state", ""),
            "reviewed": False,
            "notes": "Auto-scaffolded from the run's Evidence Vault. 'expected' and 'anchors' "
                     "are PREFILLED from the run's own extraction — VERIFY each value against "
                     "page.txt, correct it, fill every yes/no anchor with the verbatim quote, "
                     "set rubric_version to your labeling SOP's version stamp, then set "
                     "reviewed: true.",
            "rubric_version": "",
            "page_sha256": page_fingerprint(page_text),
            # Draft anchors from the run's own evidence — a human must verify or
            # replace each before the case can pass the production preflight.
            "anchors": {sid: (rec_sigs.get(sid, {}).get("evidence_text") or "")
                        for sid, st in expected.items() if st in ("yes", "no")},
            "expected": expected,
        }
        (case_dir / "labels.json").write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
        recorded = {
            "_note": "Auto-scaffolded from the run's signals for --offline replay.",
            "signals": [
                {"signal_id": s.get("signal_id"), "signal_state": s.get("signal_state", "not_found"),
                 "confidence": s.get("confidence", "low"), "evidence_text": s.get("evidence_text", ""),
                 "source_url": s.get("source_url", "")}
                for s in rec.get("signals", [])
            ],
        }
        (case_dir / "recorded_response.json").write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
        created.append(case_id)
    return created, skipped


def normalize_anchor_text(text: str) -> str:
    """THE anchor-normalization policy: lowercase, collapse all whitespace runs.

    Used identically for (a) the evaluator's anchor_rate check on model evidence
    and (b) the preflight check that every human label anchor appears in
    page.txt — one documented policy, no divergence. Mirrors the production
    verifier's normalization (enrichment/verifier.py::_normalize) so an anchor
    that passes here also passes the post-run verification pass.
    """
    return re.sub(r"\s+", " ", text.lower()).strip()


def page_fingerprint(page_text: str) -> str:
    """Fingerprint of a case's captured page text: sha256 hex of the UTF-8 text.

    Computed over the text as read with universal newlines (page.txt via
    read_text), so the value is stable across platforms. Labels record it as
    page_sha256 at labeling time; preflight recomputes and compares, catching
    a page.txt edited after its labels were authored.
    """
    return hashlib.sha256(page_text.encode("utf-8")).hexdigest()


def anchor_ok(signal: dict, page_text: str) -> bool:
    """True if a 'yes' signal's evidence_text appears in the page text under
    the documented anchor normalization (see normalize_anchor_text)."""
    ev = normalize_anchor_text(signal.get("evidence_text") or "")
    return bool(ev) and ev in normalize_anchor_text(page_text)


def score_case(case_dir: Path, expected: dict, got: dict) -> dict:
    """Compare expected vs extracted signal_state for one case; collect per-signal outcomes."""
    page_path = case_dir / "page.txt"
    page_text = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
    rows, misses = [], []
    for sid, exp_state in expected.items():
        sig = got.get(sid, {"signal_state": "not_found"})
        got_state = sig.get("signal_state", "not_found")
        ok = got_state == exp_state
        anchored = anchor_ok(sig, page_text) if got_state == "yes" else None
        rows.append({"signal_id": sid, "expected": exp_state, "got": got_state,
                     "ok": ok, "anchored": anchored})
        if not ok:
            misses.append((case_dir.name, sid, exp_state, got_state))
    return {"case": case_dir.name, "rows": rows, "misses": misses}


def _signal_groups(icp_signals: list[dict]) -> dict:
    """Classify each signal_id into 'must_have' | 'negative' | 'other' from the ICP
    flags, so per-group recall stays generic (no hardcoded signal IDs)."""
    groups = {}
    for s in icp_signals:
        sid = s.get("signal_id")
        if s.get("required_for_bullseye"):
            groups[sid] = "must_have"
        elif (s.get("positive_weight") or 0) < 0:
            groups[sid] = "negative"
        else:
            groups[sid] = "other"
    return groups


def aggregate(case_results: list[dict], groups: dict) -> dict:
    """Roll per-case rows up into the headline extraction metrics."""
    total = correct = 0
    yes_labeled = yes_caught = yes_pred = yes_pred_correct = 0
    yes_signals = anchored = 0
    grp_labeled = {"must_have": 0, "negative": 0, "other": 0}
    grp_caught = {"must_have": 0, "negative": 0, "other": 0}
    for cr in case_results:
        for r in cr["rows"]:
            total += 1
            correct += r["ok"]
            if r["expected"] == "yes":
                yes_labeled += 1
                caught = (r["got"] == "yes")
                yes_caught += caught
                grp = groups.get(r["signal_id"], "other")
                grp_labeled[grp] += 1
                grp_caught[grp] += caught
            if r["got"] == "yes":
                yes_pred += 1
                yes_pred_correct += (r["expected"] == "yes")
                yes_signals += 1
                anchored += (r["anchored"] is True)
    pct = lambda n, d: round(n / d, 4) if d else None
    return {
        "cases": len(case_results),
        "signals_compared": total,
        "state_accuracy": pct(correct, total),
        "yes_recall": pct(yes_caught, yes_labeled),
        "must_have_recall": pct(grp_caught["must_have"], grp_labeled["must_have"]),
        "exclusion_recall": pct(grp_caught["negative"], grp_labeled["negative"]),
        "other_recall": pct(grp_caught["other"], grp_labeled["other"]),
        "yes_precision": pct(yes_pred_correct, yes_pred),
        "anchor_rate": pct(anchored, yes_signals),
        "yes_labeled": yes_labeled,
        "misses": [m for cr in case_results for m in cr["misses"]],
    }


def print_report(metrics: dict, mode: str, model_note: str,
                 unreviewed: list | None = None, counts: dict | None = None) -> None:
    """Print a human-readable metrics report.

    Metrics cover reviewed cases only; `counts` breaks the golden set down into
    discovered / reviewed / excluded-unreviewed / evaluated so a shrinking
    denominator is always visible, never silent.
    """
    fmt = lambda v: "  n/a" if v is None else f"{v * 100:5.1f}%"
    print(f"\nSignal-extraction eval  ({mode})  {model_note}")
    print(f"  {datetime.now().isoformat(timespec='seconds')}")
    if counts:
        print(f"  cases: {counts['discovered']} discovered, {counts['reviewed']} reviewed, "
              f"{counts['excluded_unreviewed']} excluded (unreviewed), {counts['evaluated']} evaluated")
    print(f"  metrics over {metrics['cases']} reviewed case(s), "
          f"{metrics['signals_compared']} signals compared")
    print("  " + "-" * 46)
    print(f"  state accuracy   {fmt(metrics['state_accuracy'])}")
    print(f"  yes recall       {fmt(metrics['yes_recall'])}   (caught of {metrics['yes_labeled']} labeled-yes)")
    print(f"    must-have      {fmt(metrics['must_have_recall'])}")
    print(f"    exclusion      {fmt(metrics['exclusion_recall'])}")
    print(f"    other          {fmt(metrics['other_recall'])}")
    print(f"  yes precision    {fmt(metrics['yes_precision'])}")
    print(f"  anchor rate      {fmt(metrics['anchor_rate'])}")
    if metrics["misses"]:
        print("  " + "-" * 46)
        print(f"  {len(metrics['misses'])} mismatch(es):")
        for case, sid, exp, got in metrics["misses"]:
            print(f"    [{case}] {sid}: expected {exp!r}, got {got!r}")
    if unreviewed:
        print("  " + "-" * 46)
        print(f"  WARNING: {len(unreviewed)} unreviewed case(s) (labels not verified): {', '.join(unreviewed)}")
    print()


def check_baseline(metrics: dict, baseline: dict, unreviewed: list | None = None) -> bool:
    """Return True if every measured metric meets its floor and no case is unreviewed.

    A metric with no labeled examples in this set is skipped. A metric that IS
    measured but has no floor in the baseline fails the check — an unenforced gate
    is treated as a regression, not a silent pass (run --update-baseline to set one).
    """
    gates = {
        "state_accuracy": "min_state_accuracy",
        "must_have_recall": "min_must_have_recall",
        "exclusion_recall": "min_exclusion_recall",
        "other_recall": "min_other_recall",
        "yes_precision": "min_yes_precision",
        "anchor_rate": "min_anchor_rate",
    }
    ok = True
    print("  baseline gates:")
    for metric, key in gates.items():
        val = metrics.get(metric)
        if val is None:
            print(f"    n/a   {metric}: no labeled examples in this set (gate skipped)")
            continue
        floor = baseline.get(key)
        if floor is None:
            ok = False
            print(f"    UNPROTECTED  {metric}: {val*100:.1f}% measured but no floor in "
                  f"baseline — gate NOT enforced (run --update-baseline to set one)")
            continue
        passed = val >= floor
        ok = ok and passed
        print(f"    {'PASS' if passed else 'FAIL'}  {metric} {val*100:.1f}% >= {floor*100:.1f}%")
    if unreviewed:
        ok = False
        print(f"    FAIL  {len(unreviewed)} unreviewed case(s) — set reviewed:true after verifying labels")
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}\n")
    return ok


_PRODUCTION_CASE_COUNT = 20   # LABELING_SOP.md site-selection table
_MIN_YES_PER_SIGNAL = 4       # LABELING_SOP.md denominator rule
_VALID_STATES = {"yes", "no", "not_found"}


def _load_labels_checked(path: Path) -> tuple[dict, list[str]]:
    """Parse a labels.json, additionally reporting literal duplicate keys.

    json.loads silently keeps the last of duplicate keys; a duplicated
    signal_id in 'expected' would hide a label. Returns (labels, dup_keys).
    """
    dups: list[str] = []

    def _pairs(pairs):
        seen = set()
        for k, _ in pairs:
            if k in seen:
                dups.append(k)
            seen.add(k)
        return dict(pairs)

    labels = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    return labels, dups


def preflight_golden(cases: list[Path], icp_signals: list[dict],
                     production: bool) -> list[str]:
    """Validate the golden dataset BEFORE any tokens are spent. Returns violations.

    Production gate (the default) enforces the full LABELING_SOP.md contract:
      - exactly _PRODUCTION_CASE_COUNT reviewed cases, no unreviewed drafts;
      - every case's 'expected' keys exactly match the current ICP signal IDs
        (no missing, duplicate, unknown, or obsolete IDs), values in
        yes/no/not_found;
      - >= _MIN_YES_PER_SIGNAL true 'yes' labels per signal across the set;
      - labeling metadata present: one consistent rubric_version, and a
        page_sha256 that matches the current page.txt (a mismatch means the
        text was edited after labeling);
      - every human-labeled yes/no carries a verbatim anchor that appears in
        page.txt under normalize_anchor_text (the evaluator's own policy);
      - page.txt nonempty.

    Non-production mode (--dev-dataset, for the shipped synthetic demos and
    local experiments) keeps only the schema checks — expected keys match the
    ICP, values valid, page.txt nonempty — and relaxes count, coverage,
    metadata, and anchor requirements. It is never valid for --update-baseline.
    """
    violations: list[str] = []
    sig_ids = [s["signal_id"] for s in icp_signals]
    reviewed: list[tuple[Path, dict]] = []
    rubric_versions: dict[str, list[str]] = {}

    for case_dir in cases:
        name = case_dir.name
        try:
            labels, dup_keys = _load_labels_checked(case_dir / "labels.json")
        except (json.JSONDecodeError, OSError) as e:
            violations.append(f"[{name}] labels.json unreadable: {e}")
            continue
        if dup_keys:
            violations.append(f"[{name}] duplicate key(s) in labels.json: {', '.join(sorted(set(dup_keys)))}")
        if labels.get("reviewed") is True:
            reviewed.append((case_dir, labels))
        elif production:
            violations.append(f"[{name}] unreviewed draft in a production dataset "
                              "(verify labels and set reviewed: true, or remove the case)")

    yes_counts = {sid: 0 for sid in sig_ids}
    for case_dir, labels in reviewed:
        name = case_dir.name
        expected = labels.get("expected") or {}

        missing = [sid for sid in sig_ids if sid not in expected]
        unknown = [sid for sid in expected if sid not in sig_ids]
        if missing:
            violations.append(f"[{name}] expected is missing ICP signal(s): {', '.join(missing)}")
        if unknown:
            violations.append(f"[{name}] expected has unknown/obsolete signal(s) "
                              f"not in the current ICP: {', '.join(unknown)}")
        bad_values = {sid: st for sid, st in expected.items() if st not in _VALID_STATES}
        if bad_values:
            violations.append(f"[{name}] invalid expected value(s): "
                              + ", ".join(f"{sid}={st!r}" for sid, st in bad_values.items())
                              + " (must be yes / no / not_found)")

        page_path = case_dir / "page.txt"
        page_text = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
        if not page_text.strip():
            violations.append(f"[{name}] page.txt is missing or empty")

        for sid, st in expected.items():
            if st == "yes" and sid in yes_counts:
                yes_counts[sid] += 1

        if not production:
            continue

        rubric = (labels.get("rubric_version") or "").strip()
        if not rubric:
            violations.append(f"[{name}] missing rubric_version (stamp the SOP version "
                              "the labels were authored under)")
        else:
            rubric_versions.setdefault(rubric, []).append(name)

        recorded_fp = (labels.get("page_sha256") or "").strip()
        if not recorded_fp:
            violations.append(f"[{name}] missing page_sha256 (captured-text fingerprint)")
        elif page_text.strip() and recorded_fp != page_fingerprint(page_text):
            violations.append(f"[{name}] page_sha256 does not match page.txt — the page "
                              "text changed after labeling; re-verify labels and re-fingerprint")

        anchors = labels.get("anchors") or {}
        norm_page = normalize_anchor_text(page_text)
        for sid, st in expected.items():
            if st not in ("yes", "no"):
                continue
            anchor = (anchors.get(sid) or "").strip()
            if not anchor:
                violations.append(f"[{name}] {sid}={st} has no verbatim anchor quote "
                                  "(anchors.<signal_id>)")
            elif normalize_anchor_text(anchor) not in norm_page:
                violations.append(f"[{name}] anchor for {sid} not found in page.txt "
                                  "(compared lowercase, whitespace-collapsed)")

    if production:
        if len(reviewed) != _PRODUCTION_CASE_COUNT:
            violations.append(
                f"reviewed case count is {len(reviewed)}, production gate requires exactly "
                f"{_PRODUCTION_CASE_COUNT} (see LABELING_SOP.md site-selection table)")
        if len(rubric_versions) > 1:
            detail = "; ".join(f"{v}: {', '.join(names)}" for v, names in sorted(rubric_versions.items()))
            violations.append(f"mixed rubric_version values across cases ({detail}) — "
                              "relabel or re-verify so one rubric governs the whole set")
        short = {sid: n for sid, n in yes_counts.items() if n < _MIN_YES_PER_SIGNAL}
        for sid, n in sorted(short.items()):
            violations.append(f"signal {sid} has only {n} labeled-yes case(s); production "
                              f"gate requires >= {_MIN_YES_PER_SIGNAL} (recall is meaningless below that)")

    return violations


def main(argv: list[str] | None = None) -> int:
    """Run the golden-dataset eval and (optionally) enforce the baseline."""
    ap = argparse.ArgumentParser(description="Golden-dataset eval for signal extraction.")
    ap.add_argument("--icp", type=Path, default=_DEFAULT_ICP)
    ap.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    ap.add_argument("--golden", type=Path, default=_DEFAULT_GOLDEN)
    ap.add_argument("--baseline", type=Path, default=_DEFAULT_BASELINE)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="replay recorded_response.json (no API)")
    mode.add_argument("--live", action="store_true", help="call the real extractor (spends tokens)")
    ap.add_argument("--check", action="store_true", help="enforce baseline.json; exit 1 on regression")
    ap.add_argument("--update-baseline", action="store_true", help="write current metrics as the baseline")
    ap.add_argument("--dev-dataset", action="store_true",
                    help="EXPLICIT non-production mode: relax the dataset gate to schema checks "
                         "only (for the shipped synthetic demos / local experiments). Never valid "
                         "with --update-baseline.")
    ap.add_argument("--scaffold-from-run", type=Path, default=None, metavar="RUN_DIR",
                    help="generate golden stubs from a completed run's Evidence Vault, then exit")
    args = ap.parse_args(argv)

    if args.live and args.offline:
        ap.error("choose one of --live / --offline")
    live = args.live  # default is offline (never spends unless asked)

    # Argument-level refusals, before any work: a baseline is a production
    # artifact — it can come only from a live run over the production dataset.
    if args.update_baseline and args.dev_dataset:
        print("REFUSED: --update-baseline is never valid with --dev-dataset — a dev/demo "
              "dataset must not produce a production baseline.", file=sys.stderr)
        return 1
    if args.update_baseline and not live:
        print("REFUSED: --update-baseline requires --live. An offline replay measures the "
              "recorded responses, not the current extractor — a production baseline must "
              "come from a live extractor run. Baseline not written.", file=sys.stderr)
        return 1

    icp_signals = load_icp_signals(args.icp)

    if args.scaffold_from_run:
        created, skipped = scaffold_from_run(args.scaffold_from_run, args.golden, icp_signals)
        print(f"\nScaffolded {len(created)} case(s) into {args.golden}")
        for case_id in created:
            print(f"  + {case_id}")
        if skipped:
            print(f"  skipped {len(skipped)}:")
            for name, why in skipped:
                print(f"    - {name}: {why}")
        print("\nNext: in each labels.json, verify 'expected' and every yes/no anchor against "
              "page.txt, correct them, set rubric_version, then set reviewed: true.\n")
        return 0

    target_specialty, contact_strategy = load_run_meta(args.config)
    cases = discover_cases(args.golden)
    if not cases:
        print(f"No golden cases found under {args.golden}", file=sys.stderr)
        return 2

    # Preflight the dataset BEFORE any tokens are spent. Gating modes (--live,
    # --check, --update-baseline) refuse to proceed on a dataset that fails the
    # production contract; --dev-dataset relaxes it to schema checks only, loudly.
    if live or args.check or args.update_baseline:
        if args.dev_dataset:
            print("DEV DATASET MODE: production gate relaxed to schema checks — "
                  "these numbers are not a production quality gate.", file=sys.stderr)
        violations = preflight_golden(cases, icp_signals, production=not args.dev_dataset)
        if violations:
            print(f"PREFLIGHT FAILED — {len(violations)} violation(s), no extractor "
                  "call was made:", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 2

    # Review status is authoritative and read from DISCOVERED cases, not from
    # whichever cases happened to execute — an unreviewed draft lacking a
    # recorded_response.json must still be visible to --check, and a draft must
    # never spend live tokens or leak into metrics.
    reviewed_cases: list[Path] = []
    unreviewed: list[str] = []
    for case_dir in cases:
        labels = json.loads((case_dir / "labels.json").read_text(encoding="utf-8"))
        if labels.get("reviewed") is True:
            reviewed_cases.append(case_dir)
        else:
            unreviewed.append(case_dir.name)

    if not reviewed_cases:
        print(
            f"FAIL: {len(cases)} case(s) discovered under {args.golden} but none are "
            "reviewed: true. Verify each labels.json against page.txt per "
            "evals/LABELING_SOP.md, then set reviewed: true — draft labels never "
            "produce metrics.",
            file=sys.stderr,
        )
        return 2

    results = []
    for case_dir in reviewed_cases:
        labels = json.loads((case_dir / "labels.json").read_text(encoding="utf-8"))
        expected = labels.get("expected", {})
        if live:
            got = run_case_live(case_dir, icp_signals, target_specialty, contact_strategy)
        else:
            if not (case_dir / "recorded_response.json").exists():
                print(f"skip [{case_dir.name}]: no recorded_response.json (need --live)", file=sys.stderr)
                continue
            got = run_case_offline(case_dir, icp_signals)
        results.append(score_case(case_dir, expected, got))

    if not results:
        print(
            "FAIL: no reviewed case could be evaluated in this mode "
            "(offline replay needs recorded_response.json; use --live to run the extractor).",
            file=sys.stderr,
        )
        return 2

    counts = {
        "discovered": len(cases),
        "reviewed": len(reviewed_cases),
        "excluded_unreviewed": len(unreviewed),
        "evaluated": len(results),
    }
    metrics = aggregate(results, _signal_groups(icp_signals))
    model_note = "live: real extractor" if live else "offline: replayed responses"
    print_report(metrics, "live" if live else "offline", model_note, unreviewed, counts)

    if args.update_baseline:
        # Defense-in-depth: the production preflight above already fails on any
        # unreviewed draft, but a baseline write is the highest-stakes output of
        # this harness — a scaffold prefills 'expected' from the run's own
        # extraction, so an unverified draft is circular by design. Never let
        # one into a baseline even if the gate wiring above changes.
        if unreviewed:
            print(
                f"REFUSED: --update-baseline with {len(unreviewed)} unreviewed case(s) "
                f"({', '.join(unreviewed)}). Verify labels and set reviewed: true, or "
                "remove the drafts from the golden set. Baseline not written.",
                file=sys.stderr,
            )
            return 1
        floors = {
            "min_state_accuracy": metrics["state_accuracy"],
            "min_must_have_recall": metrics["must_have_recall"],
            "min_exclusion_recall": metrics["exclusion_recall"],
            "min_other_recall": metrics["other_recall"],
            "min_yes_precision": metrics["yes_precision"],
            "min_anchor_rate": metrics["anchor_rate"],
        }
        # Merge into any existing baseline: update the floors we have data for, but
        # never DROP a previously-set floor just because this (possibly partial) golden
        # set had no examples for it — dropping one would silently disable that gate.
        prior = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else {}
        baseline = dict(prior)
        for floor_key, floor_val in floors.items():
            if floor_val is not None:
                baseline[floor_key] = floor_val
        unset = [k for k in floors if k not in baseline]
        baseline["recorded_at"] = datetime.now().isoformat(timespec="seconds")
        baseline["mode"] = "live" if live else "offline"
        args.baseline.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"  baseline written to {args.baseline}")
        if unset:
            print(f"  WARNING: no data and no prior floor for {', '.join(unset)} — those gates stay unset")
        print()
        return 0

    if args.check:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else {}
        return 0 if check_baseline(metrics, baseline, unreviewed) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
