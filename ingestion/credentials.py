"""Professional credential tokens used to separate a provider's name from their letters.

The token list is operator-maintained data in `config/credential_tokens.json`, not
a literal in engine code, so adding a credential never requires a code change. A
run_config may replace the list (`credential_tokens`) or extend it
(`additional_credential_tokens`).
"""

import json
from functools import lru_cache
from pathlib import Path

CREDENTIAL_TOKENS_PATH = Path(__file__).resolve().parent.parent / "config" / "credential_tokens.json"


def normalize_credential_token(raw: str) -> str:
    """Normalize a token for comparison: lowercase, no dots, no spaces."""
    return (raw or "").strip().lower().replace(".", "").replace(" ", "")


@lru_cache(maxsize=1)
def default_credential_tokens() -> frozenset[str]:
    """Load the shipped credential list. Cached — the file does not change mid-run."""
    try:
        data = json.loads(CREDENTIAL_TOKENS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Credential token list unreadable at {CREDENTIAL_TOKENS_PATH}: {exc}. "
            "Provider names cannot be parsed without it."
        ) from exc
    tokens = data.get("credential_tokens") or []
    return frozenset(normalize_credential_token(t) for t in tokens if str(t).strip())


def credential_tokens(run_config: dict = None) -> frozenset[str]:
    """Resolve the credential set for a run: replace the default, and/or extend it."""
    settings = run_config or {}
    override = settings.get("credential_tokens")
    base = (frozenset(normalize_credential_token(t) for t in override if str(t).strip())
            if override is not None else default_credential_tokens())
    extra = settings.get("additional_credential_tokens") or []
    return base | frozenset(normalize_credential_token(t) for t in extra if str(t).strip())


def is_credential(part: str, tokens: frozenset[str] = None) -> bool:
    """True when a comma-separated name fragment is letters after a name, not a person."""
    token = normalize_credential_token(part)
    if not token:
        return False
    return token in (tokens if tokens is not None else default_credential_tokens())


def split_name_and_credentials(raw_name: str,
                               tokens: frozenset[str] = None) -> tuple[str, list[str]]:
    """Split "Jane Smith, MD, FACOG" into ("Jane Smith", ["MD", "FACOG"]).

    Non-credential trailing parts are kept on the name, so a genuine comma inside
    a practice name is not silently truncated.
    """
    parts = [p.strip() for p in (raw_name or "").split(",") if p.strip()]
    if not parts:
        return "", []
    resolved = tokens if tokens is not None else default_credential_tokens()
    name_parts = [parts[0]]
    credentials = []
    for part in parts[1:]:
        if is_credential(part, resolved):
            credentials.append(part)
        else:
            name_parts.append(part)
    return ", ".join(name_parts), credentials
