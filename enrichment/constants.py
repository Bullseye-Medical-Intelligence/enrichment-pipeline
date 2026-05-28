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
BASE_FIT_SCORE = 50                 # neutral starting point before signal weights
CONFIDENCE_SCORE_MAP = {"high": 90, "medium": 65, "low": 40}
NO_SIGNAL_CONFIDENCE = 30           # confidence when no signal is confirmed

# bullseye_score = FIT_WEIGHT * fit_signal_score + CONFIDENCE_WEIGHT * confidence_score
FIT_WEIGHT = 0.6
CONFIDENCE_WEIGHT = 0.4

# Tier thresholds
DEFAULT_BULLSEYE_MIN_SCORE = 75     # fallback when run_config omits bullseye_min_score
EXCLUDED_SCORE_CAP = 40             # max bullseye_score retained on excluded records

# fit_confidence_status quadrant thresholds
HIGH_FIT_THRESHOLD = 70
HIGH_CONFIDENCE_THRESHOLD = 65
