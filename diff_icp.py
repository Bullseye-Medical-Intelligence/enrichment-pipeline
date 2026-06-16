"""
diff_icp.py

Field-level diff between two ICP profiles. Given a baseline profile (A) and a
new profile (B), report which signals were added, removed, or changed (with the
exact old -> new value for every differing field), plus any profile-level
changes. No crawl, no LLM, no side effects — a pure comparison so an operator
can see exactly what moved between two ICP versions before applying one.

Two input modes feed the same core (`diff_profiles`):

    file paths:  python diff_icp.py --a v7.json --b v8.json
    stdin JSON:  echo '{"a": {...}, "b": {...}}' | python diff_icp.py

The API shells out with the stdin payload (passing the already-loaded, normalized
profiles) so all profile reading stays in one place; operators use the path form
directly. Output is a JSON object on stdout.
"""

import argparse
import json
import sys

# Signal-level fields compared in a full diff. signal_id is the match key, never
# a tracked field. Every other meaningful signal field is compared so a weight or
# flag change is never silently missed.
_SIGNAL_FIELDS = (
    "signal_label",
    "prompt_instruction",
    "positive_weight",
    "not_found_weight",
    "no_weight",
    "verification_required",
    "required_for_bullseye",
    "cap_tier",
    "floor_tier",
    "exclude_if_yes",
    "inhibited_by",
    "reinforces",
)

# Profile-level fields compared (outside the signals list).
_PROFILE_FIELDS = ("name", "version", "contact_strategy")

# Sentinel for "field absent" so we can distinguish a missing field from a field
# explicitly set to a falsy value (0, False, "").
_MISSING = object()


def _signal_map(profile: dict) -> dict:
    """Return {signal_id: signal} for a profile, skipping signals with no id."""
    out: dict = {}
    for signal in profile.get("signals") or []:
        if isinstance(signal, dict):
            sid = signal.get("signal_id")
            if sid:
                out[sid] = signal
    return out


def _field_changes(a_obj: dict, b_obj: dict, fields) -> dict:
    """Return {field: {old, new}} for every tracked field that differs.

    A field absent on one side is reported with None on that side so an added or
    removed field reads clearly in the output.
    """
    changes: dict = {}
    for field in fields:
        a_val = a_obj.get(field, _MISSING)
        b_val = b_obj.get(field, _MISSING)
        if a_val != b_val:
            changes[field] = {
                "old": None if a_val is _MISSING else a_val,
                "new": None if b_val is _MISSING else b_val,
            }
    return changes


def diff_profiles(a: dict, b: dict) -> dict:
    """Compute the field-level diff between baseline profile A and new profile B.

    Signals are matched by signal_id. Returns added / removed / changed /
    unchanged signal lists (sorted by signal_id for stable output) plus any
    profile-level field changes.
    """
    a_map = _signal_map(a)
    b_map = _signal_map(b)

    added = [
        {"signal_id": sid, "signal_label": b_map[sid].get("signal_label", "")}
        for sid in b_map
        if sid not in a_map
    ]
    removed = [
        {"signal_id": sid, "signal_label": a_map[sid].get("signal_label", "")}
        for sid in a_map
        if sid not in b_map
    ]

    changed: list = []
    unchanged: list = []
    for sid, a_sig in a_map.items():
        if sid not in b_map:
            continue
        b_sig = b_map[sid]
        field_changes = _field_changes(a_sig, b_sig, _SIGNAL_FIELDS)
        if field_changes:
            changed.append({
                "signal_id": sid,
                "signal_label": b_sig.get("signal_label", a_sig.get("signal_label", "")),
                "fields": field_changes,
            })
        else:
            unchanged.append({"signal_id": sid, "signal_label": a_sig.get("signal_label", "")})

    added.sort(key=lambda x: x["signal_id"])
    removed.sort(key=lambda x: x["signal_id"])
    changed.sort(key=lambda x: x["signal_id"])
    unchanged.sort(key=lambda x: x["signal_id"])

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "unchanged_count": len(unchanged),
        "profile_changes": _field_changes(a, b, _PROFILE_FIELDS),
    }


def _load(path: str) -> dict:
    """Load and return a JSON object from a file path."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    """Parse args (or stdin), compute the diff, print it as JSON to stdout."""
    parser = argparse.ArgumentParser(description="Diff two ICP profiles field-by-field.")
    parser.add_argument("--a", help="Path to the baseline ICP profile JSON")
    parser.add_argument("--b", help="Path to the new ICP profile JSON")
    args = parser.parse_args()

    if args.a and args.b:
        try:
            a = _load(args.a)
            b = _load(args.b)
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": f"could not read profile: {exc}"}))
            return 1
    else:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"invalid JSON payload: {exc}"}))
            return 1
        a = payload.get("a")
        b = payload.get("b")
        if not isinstance(a, dict) or not isinstance(b, dict):
            print(json.dumps({"error": "provide --a/--b paths or stdin {a, b} objects"}))
            return 1

    print(json.dumps(diff_profiles(a, b)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
