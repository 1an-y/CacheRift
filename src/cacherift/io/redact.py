"""Credential redaction shared by every component that persists or prints captures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import SplitResult, urlsplit

REDACTED = "[REDACTED]"

# Substring denylist on purpose: over-redacting a harmless header is free, under-redacting a
# gateway-specific credential header is not.
_SENSITIVE_MARKERS = (
    "authorization",
    "api-key",
    "apikey",
    "token",
    "secret",
    "cookie",
    "credential",
    "password",
    "auth",
    "key",
)


def is_sensitive_header(name: str) -> bool:
    """Return True when a header name looks like it carries a credential."""
    lowered = name.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def split_clean_url(url: str, option: str) -> SplitResult:
    """Parse a URL that will reach logs and error messages, refusing anything secret-shaped.

    Credentials in userinfo or a query string would be echoed by client libraries in error
    messages and request logs, so such URLs are rejected instead of redacted after the fact.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"{option} scheme must be http or https")
    if not parts.netloc:
        raise ValueError(f"{option} must include a host")
    if parts.username or parts.password:
        raise ValueError(f"{option} must not embed userinfo; pass credentials via environment")
    if parts.query or parts.fragment:
        raise ValueError(f"{option} must not contain a query string or fragment")
    try:
        _ = parts.port  # reading the port validates it; a bad one only fails later, per request
    except ValueError as exc:
        raise ValueError(f"{option} has an invalid port: {exc}") from exc
    return parts


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy headers, replacing the value of every credential-like header with ``[REDACTED]``."""
    return {
        name: (REDACTED if is_sensitive_header(name) else value) for name, value in headers.items()
    }


def secret_fragments(headers: Mapping[str, str]) -> tuple[str, ...]:
    """Return the raw secret strings carried by sensitive headers (auth scheme stripped)."""
    fragments: list[str] = []
    for name, value in headers.items():
        if not is_sensitive_header(name) or not value:
            continue
        fragments.append(value)
        scheme, _, remainder = value.partition(" ")
        if remainder:
            fragments.append(remainder)
    return tuple(dict.fromkeys(fragments))


def scrub_secrets(value: Any, secrets: Sequence[str]) -> Any:
    """Return a thawed copy of a JSON structure with every secret occurrence redacted.

    Operates on parsed strings rather than serialized text so JSON escaping can never hide a secret.
    """
    if not secrets:
        return value
    return _scrub(value, tuple(s for s in secrets if s))


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, Mapping):
        return {str(_scrub(k, secrets)): _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, secrets) for item in value]
    return value
