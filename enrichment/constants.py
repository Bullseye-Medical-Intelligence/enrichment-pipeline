"""
constants.py
Shared scoring constants for the enrichment pipeline. Centralised here so a
threshold change happens in exactly one place, with no drift across the
signal extractor, exclusion checker, and final scorer.
"""

# Score bounds (all scores clamp to this range)
MIN_SCORE = 0
MAX_SCORE = 100

# Fit scoring
BASE_FIT_SCORE = 50                 # neutral fallback when an ICP defines no positive weight
CONFIDENCE_SCORE_MAP = {"high": 90, "medium": 65, "low": 40}
NO_SIGNAL_CONFIDENCE = 30           # confidence when no signal is confirmed

# Fraction of a signal's positive weight credited when its state is *inferred*
# from a reinforcing signal (e.g. listed elective procedures imply cash pay)
# rather than directly observed on the site. Less than full credit because
# inference is indirect evidence.
INFERENCE_CREDIT = 0.6

# Fit credit multiplier for a confirmed "yes" signal, scaled by how strongly
# the evidence is grounded in the actual website text.
# "high" = verbatim or near-verbatim quote; "medium" = clearly implied passage;
# "low" = weak / indirect — a low-confidence yes contributes less to fit so
# an LLM that guesses at low confidence cannot manufacture a Bullseye score.
SIGNAL_CONFIDENCE_CREDIT = {"high": 1.0, "medium": 0.75, "low": 0.5}

# Minimum number of characters of extracted website text required before the
# pipeline makes an LLM signal-extraction call. Records with fewer characters
# of context receive all-not_found signals without any Claude call, preventing
# hallucinations from thin or absent web content.
MIN_CONTEXT_CHARS = 150

# bullseye_score = FIT_WEIGHT * fit_signal_score + CONFIDENCE_WEIGHT * confidence_score
FIT_WEIGHT = 0.6
CONFIDENCE_WEIGHT = 0.4

# Tier thresholds
DEFAULT_BULLSEYE_MIN_SCORE = 90     # fallback when run_config omits bullseye_min_score
EXCLUDED_SCORE_CAP = 40             # max bullseye_score retained on excluded records

# Opt-in: how many points below bullseye_min to ALSO send to GPT verification, so
# near-miss records (just under the Bullseye line) get a second opinion that can
# catch false negatives. 0 = disabled (verify Bullseye only — the default behavior).
DEFAULT_NEAR_MISS_BAND = 0

# Records scoring below this threshold have insufficient signal evidence to support
# a Contender call verdict and are assigned Manual Review instead.
# 50 ≈ one high-confidence confirmed signal of meaningful weight — the minimum
# evidence floor for a rep call. Scores 30–49 typically represent a single
# medium-confidence signal, which is too thin to justify outreach.
LOW_SCORE_MANUAL_REVIEW_THRESHOLD = 50

# fit_confidence_status quadrant thresholds
HIGH_FIT_THRESHOLD = 70
HIGH_CONFIDENCE_THRESHOLD = 65

# Confidence band boundaries (client-facing qualitative confidence).
# The band is DERIVED from the existing confidence_score — no new computation.
# >= HIGH_CONFIDENCE_THRESHOLD -> "High"; >= LOW_CONFIDENCE_THRESHOLD -> "Moderate";
# below -> "Low". LOW_CONFIDENCE_THRESHOLD=45 puts a pure low-confidence "yes"
# (confidence 40) and no-signal records (30) in "Low"; any medium evidence lifts
# to "Moderate". FLAGGED FOR REVIEW.
LOW_CONFIDENCE_THRESHOLD = 45

CONFIDENCE_BANDS = ("High", "Moderate", "Low")

# LLM generation budget for signal extraction responses.
# 8 signals × ~200 token evidence text + call_brief + sales_angle ≈ 2 000–3 500 tokens
# in practice. 8 192 gives 2× headroom for verbose sites without hitting Anthropic limits.
LLM_MAX_TOKENS = 8192


def confidence_band_for_score(confidence_score: int) -> str:
    """Map a numeric confidence_score to a qualitative band: High / Moderate / Low.

    Derivation only — does not recompute confidence. Boundaries live in constants.
    """
    if confidence_score >= HIGH_CONFIDENCE_THRESHOLD:
        return "High"
    if confidence_score >= LOW_CONFIDENCE_THRESHOLD:
        return "Moderate"
    return "Low"


# Rep call brief: the canonical key set, defined once so the signal extractor
# (which builds it) and the scorer (which defaults it) never drift.
CALL_BRIEF_STRING_FIELDS = ("why_contact", "opening_line", "likely_objection", "discovery_question", "hours_of_operation")
CALL_BRIEF_LIST_FIELDS = ("top_evidence", "missing_to_verify", "disqualifier_risk")


def empty_call_brief() -> dict:
    """Return a shaped-but-empty call_brief so the output field is always present."""
    brief = {field: "" for field in CALL_BRIEF_STRING_FIELDS}
    brief.update({field: [] for field in CALL_BRIEF_LIST_FIELDS})
    return brief
