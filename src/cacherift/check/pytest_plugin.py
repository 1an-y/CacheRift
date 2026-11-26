"""pytest integration: assert a prompt builder keeps its prefix stable, without any network."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast

import pytest

from cacherift.check.evaluator import CheckReport, evaluate_diagnoses
from cacherift.check.policy import CheckPolicy
from cacherift.cli import analyze_records
from cacherift.core.freeze import freeze_json
from cacherift.core.models import CaptureRecord, DialectName, EndpointIdentity


class PrefixSession:
    """Collects the request bodies a prompt builder produces, turn by turn.

    Every added body becomes a capture in one explicit session, so each turn's parent is the
    turn before it and lineage is never ambiguous. No request is sent anywhere.
    """

    def __init__(
        self,
        *,
        dialect: str = "openai_responses",
        model: str = "gpt-6.1",
        provider: str = "unknown",
        endpoint_path: str = "",
    ) -> None:
        self._identity = EndpointIdentity(
            dialect=cast(DialectName, dialect),
            provider=provider,
            model=model,
            endpoint_path=endpoint_path,
        )
        self._session_id = "pytest-cacherift-session"
        self._records: list[CaptureRecord] = []

    @property
    def records(self) -> tuple[CaptureRecord, ...]:
        return tuple(self._records)

    def add(
        self,
        body: Mapping[str, Any] | bytes,
        *,
        response: Mapping[str, Any] | None = None,
    ) -> CaptureRecord:
        """Add one turn: a parsed request body, or the raw bytes it would put on the wire."""
        number = len(self._records) + 1
        raw = body if isinstance(body, bytes) else None
        parsed: Any = json.loads(raw) if raw is not None else body
        record = CaptureRecord(
            record_id=f"turn-{number:04d}",
            timestamp=float(number),
            identity=self._identity,
            request_headers=freeze_json({}),
            captured_body_bytes=raw,
            parsed_body=freeze_json(parsed),
            response_body=freeze_json(dict(response)) if response is not None else None,
            client_session_id=self._session_id,
            parent_record_id=self._records[-1].record_id if self._records else None,
        )
        self._records.append(record)
        return record

    def report(self, policy: CheckPolicy | None = None) -> CheckReport:
        return evaluate_diagnoses(analyze_records(self._records), policy or CheckPolicy.default())

    def assert_stable(self, policy: CheckPolicy | None = None) -> CheckReport:
        return assert_prefix_stable(self._records, policy=policy)


def assert_prefix_stable(
    records: Sequence[CaptureRecord], policy: CheckPolicy | None = None
) -> CheckReport:
    """Raise AssertionError when the turns break their own prefix; otherwise return the report.

    The message names each blocking turn by record id, rule and JSON path. It never contains
    prompt text.
    """
    report = evaluate_diagnoses(analyze_records(records), policy or CheckPolicy.default())
    if report.errors:
        lines = [
            f"  {finding.record_id} (line {finding.line}): {finding.rule} "
            f"{finding.json_path or ''}".rstrip()
            for finding in report.errors
        ]
        raise AssertionError(
            f"prompt cache prefix is unstable, {len(report.errors)} error(s):\n"
            + "\n".join(lines)
        )
    return report


@pytest.fixture
def cacherift_session() -> Iterator[PrefixSession]:
    """Collect prompt-builder turns with ``add()``, then call ``assert_stable()`` on the session."""
    session = PrefixSession()
    yield session
