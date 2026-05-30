"""
narrative_generator.py

Stage 3 of ICP generation: generate the ICP narrative package from the approved
signal set. This stage runs ONLY after the operator approves signals — never at
draft time. Demo accounts are grounded in the final approved signals.
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

_PROMPT_PATH = Path(__file__).parent / "prompts" / "narrative_v1.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


class NarrativePackage(BaseModel):
    """Output of Stage 3: ICP description, hypothesis, and demo accounts."""

    description: str
    hypothesis: dict
    demo_accounts: list[dict]


def generate_narrative(brief: ProductBrief, approved_signals: list[dict]) -> NarrativePackage:
    """Generate ICP narrative and demo accounts from the approved signal set.

    Raises ValueError on API error or unparseable response.
    """
    must_have_labels = [
        s["signal_label"] for s in approved_signals if s.get("required_for_bullseye")
    ]
    friction_labels = [
        s["signal_label"] for s in approved_signals
        if isinstance(s.get("positive_weight"), (int, float)) and s["positive_weight"] < 0
    ]

    prompt = _PROMPT_TEMPLATE.format(
        product_name=brief.product_name,
        what_it_is=brief.what_it_is,
        clinical_workflow_fit=brief.clinical_workflow_fit,
        commercial_positioning=brief.commercial_positioning,
        target_practice_traits=", ".join(brief.target_practice_traits),
        approved_signals_json=json.dumps(approved_signals, indent=2),
        must_have_labels=", ".join(must_have_labels) if must_have_labels else "None declared",
        friction_labels=", ".join(friction_labels) if friction_labels else "None declared",
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            timeout=120,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise ValueError(f"Claude API error during narrative generation: {exc}") from exc

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Narrative generator returned non-JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Narrative generator must return a JSON object.")

    description = str(data.get("description") or "").strip()
    hypothesis = data.get("hypothesis") or {}
    demo_accounts = data.get("demo_accounts") or []

    if not description:
        raise ValueError("Narrative generator response missing 'description'.")
    if not isinstance(hypothesis, dict):
        raise ValueError("Narrative generator response missing 'hypothesis' object.")
    if not isinstance(demo_accounts, list) or len(demo_accounts) != 3:
        raise ValueError("Narrative generator must return exactly 3 demo accounts.")

    logger.info(
        "Generated narrative package for '%s' with %d demo accounts.",
        brief.product_name, len(demo_accounts),
    )
    return NarrativePackage(
        description=description,
        hypothesis=hypothesis,
        demo_accounts=demo_accounts,
    )
