"""Secret redaction at write boundaries.

Backend agent prompts, outputs, handoff notes, and completion commands
can all contain API keys or tokens, and without redaction those land
verbatim in .skein/events.ndjson and .skein/evidence/. This module
masks common secret shapes before storage. It is applied at the write
boundary only (event-log payload writer, evidence file writers); data
already stored is never mutated.

Properties:
- Idempotent: redacting redacted text is a no-op.
- Conservative on prose: only shaped secrets and explicit key=value
  assignments are masked. Ordinary sentences containing the words
  "password" or "secret" pass through untouched.
- The replacement token itself matches no pattern.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

REDACTED = "[REDACTED]"

# (pattern, replacement). Assignment patterns keep the key name so the
# shape of the config stays readable; everything else is fully masked.
# sk-ant- is listed before the generic sk- so the longer shape wins.
_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),  # AWS access key ID
    (re.compile(r"gh[po]_[A-Za-z0-9]{16,}"), REDACTED),  # GitHub tokens
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"), REDACTED),  # Anthropic keys
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), REDACTED),  # generic sk- API keys
    (re.compile(r"xox[bp]-[A-Za-z0-9\-]{10,}"), REDACTED),  # Slack tokens
    (re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
        r".*?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL), REDACTED),  # PEM private key blocks
    (re.compile(
        r"(?i)\b(password|api_key)\s*=\s*"
        r"(?:'[^']*'|\"[^\"]*\"|[^\s'\";,&]+)"),
     r"\1=" + REDACTED),  # password=/api_key= assignments
]


def redact_with_count(text: str) -> Tuple[str, int]:
    """Return (redacted_text, number_of_replacements).

    Non-string input is returned unchanged with a count of 0.
    Idempotent: redact_with_count(redact_secrets(t))[0] == redact_secrets(t).
    """
    if not isinstance(text, str) or not text:
        return text, 0
    total = 0
    for pattern, replacement in _PATTERNS:
        text, n = pattern.subn(replacement, text)
        total += n
    return text, total


def redact_secrets(text: str) -> str:
    """Mask secret shapes in text. See module docstring for guarantees."""
    redacted, _ = redact_with_count(text)
    return redacted


def redact_payload(obj: Any) -> Tuple[Any, int]:
    """Deep-copy a JSON-shaped payload with every string redacted.

    Returns (redacted_copy, hit_count). Dict keys are redacted too;
    non-string leaves pass through by reference (they are immutable
    JSON scalars).
    """
    if isinstance(obj, str):
        return redact_with_count(obj)
    if isinstance(obj, dict):
        total = 0
        out: Dict[Any, Any] = {}
        for key, value in obj.items():
            new_key, key_hits = (redact_with_count(key)
                                 if isinstance(key, str) else (key, 0))
            new_value, value_hits = redact_payload(value)
            out[new_key] = new_value
            total += key_hits + value_hits
        return out, total
    if isinstance(obj, list):
        total = 0
        items = []
        for value in obj:
            new_value, hits = redact_payload(value)
            items.append(new_value)
            total += hits
        return items, total
    return obj, 0


def write_redacted(path: str | Path, text: str) -> int:
    """Write text to path with secrets masked. Returns the hit count."""
    redacted, hits = redact_with_count(text)
    Path(path).write_text(redacted, encoding="utf-8")
    return hits
