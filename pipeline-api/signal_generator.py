"""
signal_generator.py

Stage 2 of ICP generation: adjust the specialty anchor signal template to a
specific product using the compressed product brief. This is the only stage
that re-runs during the operator's edit-and-refine loop.

Reads the product brief (Stage 1 output) and the anchor template for the matched
specialty; never re-sends the raw crawl.
"""

import json
import logging
import re
from pathlib import Path

import anthropic
from pydantic import BaseModel

import config
from crawl_compressor import ProductBrief

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "signal_adjust_v1.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")

_TEMPLATES_DIR = Path(__file__).parent / "icp_templates"

# Keyword -> template file mapping. Matched against the lowercased specialty string.
_SPECIALTY_MAP: list[tuple[tuple[str, ...], str]] = [
    (("ob", "gyn", "fertility", "reproductive", "rei", "infertil"), "obgyn_fertility"),
    (("aesthet", "cosmetic", "dermatol"), "aesthetics"),
    (("urol",), "urology"),
    (("orthop", "ortho", "musculosk"), "orthopedics"),
]

_REQUIRED_SIGNAL_FIELDS = frozenset({
    "signal_id", "signal_label", "prompt_instruction",
    "positive_weight", "no_weight", "not_found_weight",
    "required_for_bullseye", "source_type",
})

_INVERSE_BILLING_TERMS = (
    "insurance only",
    "insurance based only",
    "only accepts insurance",
    "insurance exclusive",
)


class SignalSet(BaseModel):
    """Output of Stage 2: the adjusted signal list for this product."""

    specialty: str
    signals: list[dict]


def _resolve_template(specialty: str) -> dict:
    """Return the anchor template dict for the given specialty string.

    Falls back to an empty template if no match - Stage 2 will generate from
    the product brief alone, which is still better than the old monolithic call.
    """
    slug = specialty.lower()
    for keywords, template_name in _SPECIALTY_MAP:
        if any(kw in slug for kw in keywords):
            path = _TEMPLATES_DIR / f"{template_name}.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("signals"):
                    return data
                logger.warning("Template '%s' is a stub with no signals.", template_name)
                return data
    logger.warning("No anchor template found for specialty '%s'. Generating without anchor.", specialty)
    return {"specialty": slug, "specialty_label": specialty, "signals": []}


def generate_signals(brief: ProductBrief, specialty: str) -> SignalSet:
    """Adjust the anchor signal template to this product using the product brief.

    Raises ValueError on API error or unparseable response.
    """
    template = _resolve_template(specialty)
    anchor_signals_json = json.dumps(template.get("signals", []), indent=2)
    specialty_label = template.get("specialty_label", specialty)

    prompt = _PROMPT_TEMPLATE.format(
        product_name=brief.product_name,
        what_it_is=brief.what_it_is,
        clinical_workflow_fit=brief.clinical_workflow_fit,
        commercial_positioning=brief.commercial_positioning,
        target_practice_traits=", ".join(brief.target_practice_traits),
        specialty_label=specialty_label,
        anchor_signals_json=anchor_signals_json,
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            timeout=90,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise ValueError(f"Claude API error during signal generation: {exc}") from exc

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        signals = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Signal generator returned non-JSON: {exc}") from exc

    if not isinstance(signals, list):
        raise ValueError("Signal generator must return a JSON array.")

    signals = _drop_redundant_billing_inverse_signals(signals)
    _validate_signals(signals)
    logger.info("Generated %d signals for '%s' / '%s'.", len(signals), brief.product_name, specialty)
    return SignalSet(specialty=template.get("specialty", specialty), signals=signals)


def _drop_redundant_billing_inverse_signals(signals: list[dict]) -> list[dict]:
    """Remove standalone inverse billing signals from ICP drafts.

    Payment readiness belongs in the primary cash-pay / out-of-network signal.
    A separate inverse payer-model row is redundant and double-counts the same
    commercial fact.
    """
    filtered = []
    for sig in signals:
        if _is_redundant_billing_inverse_signal(sig):
            logger.info(
                "Dropped redundant inverse billing signal '%s'.",
                sig.get("signal_id", sig.get("signal_label", "unknown")),
            )
            continue
        filtered.append(sig)
    return filtered


def _is_redundant_billing_inverse_signal(signal: dict) -> bool:
    """Return True for payer-model inverse signals that should not be standalone."""
    text = _normalized_signal_text(signal)
    if not any(term in text for term in _INVERSE_BILLING_TERMS):
        return False
    return any(term in text for term in ("billing", "model", "payer", "payor", "insurance"))


def _normalized_signal_text(signal: dict) -> str:
    """Combine human-facing signal fields into normalized text for classifiers."""
    parts = [
        signal.get("signal_label"),
        signal.get("prompt_instruction"),
        signal.get("notes"),
        signal.get("note"),
    ]
    text = " ".join(str(p) for p in parts if p is not None).lower()
    return re.sub(r"[-_/]+", " ", text)


def _validate_signals(signals: list[dict]) -> None:
    """Raise ValueError if any signal is missing required fields or has zero no_weight on positive signals."""
    for i, sig in enumerate(signals):
        missing = _REQUIRED_SIGNAL_FIELDS - set(sig.keys())
        if missing:
            raise ValueError(f"Signal {i} missing fields: {missing}")
        if not isinstance(sig.get("positive_weight"), (int, float)):
            raise ValueError(f"Signal {i} positive_weight must be numeric.")
        if not isinstance(sig.get("no_weight"), (int, float)):
            raise ValueError(f"Signal {i} no_weight must be numeric.")
        if not isinstance(sig.get("required_for_bullseye"), bool):
            raise ValueError(f"Signal {i} required_for_bullseye must be bool.")
        pw = sig["positive_weight"]
        nw = sig["no_weight"]
        if pw > 0 and nw == 0:
            logger.warning(
                "Signal '%s' has positive_weight=%s but no_weight=0. "
                "Stage 2 should set a confirmed-absent penalty for core requirements.",
                sig.get("signal_id"), pw,
            )
