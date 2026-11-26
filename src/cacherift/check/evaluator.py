"""Pure evaluation of turn diagnoses against a check policy."""

from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cacherift.check.policy import ECHO_CAUSE_TO_CHANGE, CheckPolicy, Severity, normalize_rule
from cacherift.core.models import LineageStatus, TurnDiagnosis, UsageState

_USAGE_RULES: dict[str, str] = {
    UsageState.INVALID.value: "usage_invalid",
    UsageState.MISSING.value: "usage_missing",
    UsageState.INCOMPLETE.value: "usage_incomplete",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One policy verdict. ``line`` is the record's 1-based position in the capture."""

    record_id: str
    line: int
    rule: str
    severity: Severity
    source: str
    json_path: str | None = None
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "record_id": self.record_id,
            "severity": self.severity,
            "rule": self.rule,
            "source": self.source,
            "json_path": self.json_path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CheckReport:
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def ignored(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "ignore")

    @property
    def summary(self) -> dict[str, int]:
        return {
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "ignored": len(self.ignored),
        }

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


@functools.lru_cache(maxsize=256)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Glob over JSON paths: ``[*]`` is any index, a bare ``*`` does not cross a dot."""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("[*]", index):
            parts.append(r"\[\d+\]")
            index += 3
        elif pattern[index] == "*":
            parts.append("[^.]*")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts))


def path_matches(json_path: str, pattern: str) -> bool:
    return _compile_pattern(pattern).fullmatch(json_path) is not None


def evaluate_diagnoses(diagnoses: Sequence[TurnDiagnosis], policy: CheckPolicy) -> CheckReport:
    """Apply ``policy`` to each turn in capture order. Pure: no I/O, no prompt text is read."""
    findings: list[Finding] = []
    for line, diagnosis in enumerate(diagnoses, start=1):
        record_id = diagnosis.record_id

        for change in diagnosis.observed_changes:
            rule = normalize_rule(change.change_kind.value)
            if any(path_matches(change.json_path, pattern) for pattern in policy.ignore_paths):
                findings.append(
                    Finding(record_id, line, rule, "ignore", "observed_change", change.json_path)
                )
                continue
            severity = policy.severity(rule)
            if severity is not None:
                findings.append(
                    Finding(record_id, line, rule, severity, "observed_change", change.json_path)
                )

        # A cause that merely restates an observed change is reported through that change.
        for cause in diagnosis.candidate_causes:
            if cause.code in ECHO_CAUSE_TO_CHANGE:
                continue
            severity = policy.severity(cause.code)
            if severity is not None:
                findings.append(
                    Finding(
                        record_id,
                        line,
                        normalize_rule(cause.code),
                        severity,
                        "candidate_cause",
                        None,
                        cause.summary,
                    )
                )

        usage_rule = _USAGE_RULES.get(diagnosis.usage.state.value)
        if usage_rule is not None:
            severity = policy.severity(usage_rule)
            if severity is not None:
                notes = ", ".join(diagnosis.usage.notes) or "no_details"
                message = f"usage is {diagnosis.usage.state.value} ({notes})"
                findings.append(
                    Finding(record_id, line, usage_rule, severity, "usage", None, message)
                )

        if diagnosis.lineage.status is LineageStatus.NO_BASELINE:
            severity = policy.severity("no_baseline")
            if severity is not None:
                findings.append(
                    Finding(
                        record_id,
                        line,
                        "no_baseline",
                        severity,
                        "lineage",
                        None,
                        "no local parent baseline was captured before this turn",
                    )
                )

    return CheckReport(tuple(findings))
