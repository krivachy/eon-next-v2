"""Secret redaction for log lines, exception text and diagnostics.

E.ON's GraphQL validation errors can echo submitted variables back verbatim
(including the API key), and transport errors can carry request details.
Anything derived from API responses or exceptions that reaches a log line or
an exception message goes through :func:`redact_secrets` first.
"""

from __future__ import annotations

import re
from typing import Any

# Config-entry / payload keys whose values must never be shown.  Suitable for
# ``homeassistant.components.diagnostics.async_redact_data``.
TO_REDACT: frozenset[str] = frozenset(
    {
        "api_key",
        "APIKey",
        "apiKey",
        "password",
        "refresh_token",
        "refreshToken",
        "token",
        "authorization",
        "Authorization",
    }
)

REDACTED = "**REDACTED**"

# ``key: value`` / ``key=value`` / ``"key": "value"`` pairs for secret-ish keys.
_KEY_VALUE_RE = re.compile(
    r"""(?P<key>['"]?\b(?:api_?key|refresh_?token|token|password|secret|authorization)\b['"]?\s*[:=]\s*)"""
    r"""(?P<val>'[^']*'|"[^"]*"|(?:JWT|Bearer)\s+[^\s,;}\]'"]+|[^\s,;}\]'"]+)""",
    re.IGNORECASE,
)

_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Kraken API keys.
    (re.compile(r"sk_(?:live|test)_[A-Za-z0-9_\-]+"), REDACTED),
    # JWTs (access tokens).
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"), REDACTED),
    # Authorization header values.
    (re.compile(r"\b(?:JWT|Bearer)\s+[A-Za-z0-9_\-.]{8,}"), REDACTED),
    # Email addresses (personal data; the old login identifier).
    (re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+"), REDACTED),
)


def redact_secrets(value: Any) -> str:
    """Return ``value`` as text with API keys, tokens and emails masked."""
    text = value if isinstance(value, str) else str(value)

    def _mask(match: re.Match[str]) -> str:
        val = match.group("val")
        quote = val[0] if val[:1] in ("'", '"') else ""
        return f"{match.group('key')}{quote}{REDACTED}{quote}"

    text = _KEY_VALUE_RE.sub(_mask, text)
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redacted_error_messages(errors: Any) -> list[str] | str:
    """Return redacted GraphQL error messages, or a placeholder when absent."""
    if not isinstance(errors, list):
        return "Unknown error"
    messages = [
        redact_secrets(e.get("message", "error")) for e in errors if isinstance(e, dict)
    ]
    return messages or "Unknown error"
