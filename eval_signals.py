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
    labels.json             required. practice fields + expected signal_state per signal_id:
                              { "practice_name": "...", "website_url": "...",
                                "specialty": "OBGYN", "address_city": "...", "address_state": "...",
                                "expected": { "cash_pay_signal": "yes", "ivf_listed": "no", ... } }
    page.txt                the captured website text. Required for --live and for the
                              evidence-anchor check. (Tip: copy a real Evidence Vault snapshot.)
    recorded_response.json  optional. A saved model signals list, replayed by --offline:
                              { "signals": [ { "signal_id": "...", "signal_state": "yes",
                                              "confidence": "high", "evidence_text": "...",
                                              "source_url": "..." }, ... ] }

MODES
    --offline   replay recorded_response.json through the REAL validator. No API,
                fully deterministic. Use it in CI and to self-test the harness.
    --live      call the real extractor (Claude) on page.txt. Spends tokens.
    --check     enforce the thresholds in evals/baseline.json; exit 1 on regression.
    --update-baseline   write current metrics into the baseline file.

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
import json
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


def anchor_ok(signal: dict, page_text: str) -> bool:
    """True if a 'yes' signal's evidence_text appears verbatim in the page text."""
    ev = (signal.get("evidence_text") or "").strip().lower()
    return bool(ev) and ev in page_text.lower()


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


def aggregate(case_results: list[dict]) -> dict:
    """Roll per-case rows up into the headline extraction metrics."""
    total = correct = 0
    yes_labeled = yes_caught = yes_pred = yes_pred_correct = 0
    yes_signals = anchored = 0
    for cr in case_results:
        for r in cr["rows"]:
            total += 1
            correct += r["ok"]
            if r["expected"] == "yes":
                yes_labeled += 1
                yes_caught += (r["got"] == "yes")
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
        "yes_precision": pct(yes_pred_correct, yes_pred),
        "anchor_rate": pct(anchored, yes_signals),
        "yes_labeled": yes_labeled,
        "misses": [m for cr in case_results for m in cr["misses"]],
    }


def print_report(metrics: dict, mode: str, model_note: str) -> None:
    """Print a human-readable metrics report."""
    fmt = lambda v: "  n/a" if v is None else f"{v * 100:5.1f}%"
    print(f"\nSignal-extraction eval  ({mode})  {model_note}")
    print(f"  {datetime.now().isoformat(timespec='seconds')}")
    print(f"  cases: {metrics['cases']}   signals compared: {metrics['signals_compared']}")
    print("  " + "-" * 46)
    print(f"  state accuracy   {fmt(metrics['state_accuracy'])}")
    print(f"  yes recall       {fmt(metrics['yes_recall'])}   (caught of {metrics['yes_labeled']} labeled-yes)")
    print(f"  yes precision    {fmt(metrics['yes_precision'])}")
    print(f"  anchor rate      {fmt(metrics['anchor_rate'])}")
    if metrics["misses"]:
        print("  " + "-" * 46)
        print(f"  {len(metrics['misses'])} mismatch(es):")
        for case, sid, exp, got in metrics["misses"]:
            print(f"    [{case}] {sid}: expected {exp!r}, got {got!r}")
    print()


def check_baseline(metrics: dict, baseline: dict) -> bool:
    """Return True if every metric meets its baseline floor; print each gate."""
    gates = {
        "state_accuracy": "min_state_accuracy",
        "yes_recall": "min_yes_recall",
        "anchor_rate": "min_anchor_rate",
    }
    ok = True
    print("  baseline gates:")
    for metric, key in gates.items():
        floor = baseline.get(key)
        val = metrics.get(metric)
        if floor is None:
            continue
        passed = val is not None and val >= floor
        ok = ok and passed
        print(f"    {'PASS' if passed else 'FAIL'}  {metric} {('%.1f%%' % (val*100)) if val is not None else 'n/a'} "
              f">= {floor*100:.1f}%")
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}\n")
    return ok


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
    args = ap.parse_args(argv)

    if args.live and args.offline:
        ap.error("choose one of --live / --offline")
    live = args.live  # default is offline (never spends unless asked)

    icp_signals = load_icp_signals(args.icp)
    target_specialty, contact_strategy = load_run_meta(args.config)
    cases = discover_cases(args.golden)
    if not cases:
        print(f"No golden cases found under {args.golden}", file=sys.stderr)
        return 2

    results = []
    for case_dir in cases:
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

    metrics = aggregate(results)
    model_note = "live: real extractor" if live else "offline: replayed responses"
    print_report(metrics, "live" if live else "offline", model_note)

    if args.update_baseline:
        baseline = {
            "min_state_accuracy": metrics["state_accuracy"],
            "min_yes_recall": metrics["yes_recall"],
            "min_anchor_rate": metrics["anchor_rate"],
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "live" if live else "offline",
        }
        args.baseline.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"  baseline written to {args.baseline}\n")
        return 0

    if args.check:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else {}
        return 0 if check_baseline(metrics, baseline) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
