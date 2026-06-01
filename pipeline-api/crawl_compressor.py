"""
crawl_compressor.py

Stage 1 of ICP generation: compress raw crawled website text into a structured
product brief. Runs once per product setup; every downstream stage reads the
brief instead of the raw crawl.
"""

import json
import logging
import re
from pathlib import Path

import anthropic
from pydantic import BaseModel

import config

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "crawl_compress_v1.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


class ProductBrief(BaseModel):
    """Compressed product brief produced by Stage 1.

    Stored as product_brief on the ICP profile JSON. Never exceeds ~200 words.
    Passed to Stage 2 (signal_generator) and Stage 3 (narrative_generator)
    instead of the raw crawl text.
    """

    product_name: str
    what_it_is: str
    clinical_workflow_fit: str
    commercial_positioning: str
    target_practice_traits: list[str]


def compress_crawl(
    company_name: str,
    product_name: str,
    specialty: str,
    description: str,
    company_page_text: str,
    product_page_text: str,
    product_type: str = "",
    focus_areas: str = "",
    exclusion_notes: str = "",
) -> ProductBrief:
    """Call Claude to compress raw crawl text into a structured product brief.

    product_type, focus_areas, and exclusion_notes are operator-provided steering
    inputs from the ICP builder form; they shape target_practice_traits so the
    builder's visible fields actually influence the draft.

    Raises ValueError on API error or unparseable response.
    """
    prompt = _PROMPT_TEMPLATE.format(
        company_name=company_name or "Not specified",
        product_name=product_name,
        product_type=product_type or "Not specified",
        specialty=specialty,
        description=description or "Not provided",
        focus_areas=focus_areas or "None provided",
        exclusion_notes=exclusion_notes or "None provided",
        company_page_text=company_page_text or "Not available",
        product_page_text=product_page_text or "Not available",
    )
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            timeout=60,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise ValueError(f"Claude API error during crawl compression: {exc}") from exc

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Crawl compressor returned non-JSON: {exc}") from exc

    required = {"product_name", "what_it_is", "clinical_workflow_fit",
                "commercial_positioning", "target_practice_traits"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"Crawl compressor response missing fields: {missing}")

    if not isinstance(data.get("target_practice_traits"), list):
        raise ValueError("target_practice_traits must be a list")

    logger.info("Crawl compressed to product brief for '%s'.", product_name)
    return ProductBrief(**{k: data[k] for k in required})
