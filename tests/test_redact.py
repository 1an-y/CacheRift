from __future__ import annotations

from cacherift.core.freeze import freeze_json
from cacherift.io.redact import REDACTED, redact_headers, scrub_secrets, secret_fragments

_SECRET_HEADERS = {
    "authorization": "Bearer sk-live-1",
    "X-Api-Key": "sk-live-1",
    "x-goog-api-key": "AIza-1",
    "cookie": "session=abc",
    "proxy-authorization": "Basic dXNlcjpwYXNz",
}
_PLAIN_HEADERS = {
    "content-type": "application/json",
    "accept": "*/*",
    "anthropic-version": "2023-06-01",
    "x-cacherift-session-id": "sess-1",
}


def test_redact_headers_replaces_credential_like_values_and_keeps_the_rest() -> None:
    headers = {**_PLAIN_HEADERS, **_SECRET_HEADERS}
    redacted = redact_headers(headers)

    assert redacted == {**_PLAIN_HEADERS, **{name: REDACTED for name in _SECRET_HEADERS}}
    assert headers["authorization"] == "Bearer sk-live-1"  # the input is left untouched


def test_secret_fragments_strip_the_auth_scheme_and_deduplicate() -> None:
    fragments = secret_fragments(
        {"authorization": "Bearer sk-live-1", "x-api-key": "sk-live-1", "accept": "gzip"}
    )

    assert fragments == ("Bearer sk-live-1", "sk-live-1")


def test_scrub_secrets_replaces_every_string_occurrence_at_any_depth() -> None:
    body = freeze_json(
        {"error": {"message": "bad key sk-live-1", "nested": ["sk-live-1", 1, None, True]}}
    )

    scrubbed = scrub_secrets(body, ["sk-live-1"])

    assert scrubbed == {
        "error": {"message": f"bad key {REDACTED}", "nested": [REDACTED, 1, None, True]}
    }
    assert body["error"]["message"] == "bad key sk-live-1"  # frozen input is not mutated


def test_scrub_secrets_without_secrets_returns_the_value_unchanged() -> None:
    body = freeze_json({"usage": {"input_tokens": 10}})

    assert scrub_secrets(body, []) is body
