"""Audited repair engine: hard safety valves, whitelisted rules in a fixed order, hash trail."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from cacherift.core.freeze import canonical_project_bytes, freeze_json, sha256_hex, thaw_json
from cacherift.core.models import CaptureRecord, LineageStatus, TurnDiagnosis
from cacherift.repair.models import PROJECTED, WIRE_RAW, RepairAuditEntry, RepairEdit, RepairResult
from cacherift.repair.rules import ALL_RULES, RULES, ParentTurn

AMBIGUOUS_LINEAGE = "ambiguous_lineage"
UNSUPPORTED_CAPABILITY = "unsupported_capability"
PARENT_NOT_REPAIRED = "parent_not_repaired"
# The capability-table fact the diagnoser relies on too: top-level instructions come last.
_INSTRUCTIONS_AFTER_INPUT = ("tools", "input", "instructions")


def _applies(rule: str, diagnosis: TurnDiagnosis, parent_body: dict[str, Any] | None) -> bool:
    if rule == "migrate_gpt62_top_level_instructions":
        return diagnosis.identity.dialect == "openai_responses" and (
            tuple(diagnosis.capability.unit_order) == _INSTRUCTIONS_AFTER_INPUT
        )
    if rule == "normalize_anthropic_breakpoints":
        return diagnosis.identity.dialect == "anthropic_messages" and parent_body is not None
    return True


def _body_of(record: CaptureRecord) -> dict[str, Any]:
    body = thaw_json(record.parsed_body)
    return body if isinstance(body, dict) else {}


def _top_level_instructions(body: Any) -> str | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get("instructions")
    return value if isinstance(value, str) and value else None


def _held_back_by_parent(body: dict[str, Any], parent: ParentTurn) -> bool:
    """The turn has static top-level instructions, but its parent still carries them unmigrated."""
    instructions = _top_level_instructions(body)
    return (
        instructions is not None
        and parent.body is not None
        and "instructions" in parent.body
        and parent.top_level_instructions == instructions
    )


def _entry(
    line: int,
    diagnosis: TurnDiagnosis,
    original: CaptureRecord,
    body: dict[str, Any],
    *,
    applied: bool,
    blocked: str | None,
    edits: tuple[RepairEdit, ...],
) -> RepairAuditEntry:
    # Before: the wire bytes when the capture has them, else the order-preserving projection.
    raw = original.captured_body_bytes
    if raw is not None:
        before_space, before_bytes = WIRE_RAW, raw
    else:
        before_space, before_bytes = PROJECTED, canonical_project_bytes(original.parsed_body)
    after_bytes = canonical_project_bytes(body) if applied else None
    return RepairAuditEntry(
        record_id=original.record_id,
        line=line,
        lineage_status=diagnosis.lineage.status.value,
        parent_record_id=diagnosis.lineage.parent_record_id,
        support_level=diagnosis.capability.support_level,
        applied=applied,
        blocked_reason=blocked,
        edits=edits,
        before_space=before_space,
        before_sha256=sha256_hex(before_bytes),
        before_len=len(before_bytes),
        after_sha256=sha256_hex(after_bytes) if after_bytes is not None else None,
        after_len=len(after_bytes) if after_bytes is not None else None,
    )


def repair_records(
    records: Sequence[CaptureRecord],
    diagnoses: Sequence[TurnDiagnosis],
    *,
    rules: Iterable[str] = ALL_RULES,
) -> RepairResult:
    """Rewrite what the enabled whitelisted rules allow; refuse everything else, with a reason.

    Pure: returns new records and an audit trail, writes nothing, never mutates its input. Each
    turn is compared against its parent's *repaired* body, so a whole lineage ends up consistent.
    """
    if len(records) != len(diagnoses):
        raise ValueError("records and diagnoses must line up one to one")
    selected = set(rules)
    unknown = sorted(selected - set(ALL_RULES))
    if unknown:
        raise ValueError(
            f"unknown repair rule(s): {', '.join(unknown)}; valid rules: {', '.join(ALL_RULES)}"
        )

    originals = {record.record_id: record for record in records}
    effective: dict[str, dict[str, Any]] = {}
    output: list[CaptureRecord] = []
    audit: list[RepairAuditEntry] = []

    for line, (record, diagnosis) in enumerate(zip(records, diagnoses, strict=True), start=1):
        body = _body_of(record)
        parent_id = diagnosis.lineage.parent_record_id
        parent_body = effective.get(parent_id) if parent_id is not None else None
        parent_original = originals.get(parent_id) if parent_id is not None else None
        parent_text = None
        if parent_original is not None:
            parent_text = _top_level_instructions(parent_original.parsed_body)
        parent = ParentTurn(parent_body, parent_text)

        edits: list[RepairEdit] = []
        blocked: str | None = None
        if diagnosis.lineage.status is LineageStatus.AMBIGUOUS:
            # Without a trustworthy parent nothing can be aligned to it.
            blocked = AMBIGUOUS_LINEAGE
        elif diagnosis.capability.support_level != "full":
            offered = [
                name
                for name in ALL_RULES
                if name in selected
                and _applies(name, diagnosis, parent_body)
                and RULES[name](body, parent) is not None
            ]
            if offered:
                blocked = UNSUPPORTED_CAPABILITY
        else:
            for name in ALL_RULES:
                if name in selected and _applies(name, diagnosis, parent_body):
                    outcome = RULES[name](body, parent)
                    if outcome is not None:
                        body, found = outcome
                        edits.extend(found)
            if (
                not edits
                and "migrate_gpt62_top_level_instructions" in selected
                and _applies("migrate_gpt62_top_level_instructions", diagnosis, parent_body)
                and _held_back_by_parent(body, parent)
            ):
                # Migrating this turn alone would put a new unit in front of its parent's prefix.
                blocked = PARENT_NOT_REPAIRED

        effective[record.record_id] = body
        repaired = (
            replace(record, captured_body_bytes=None, parsed_body=freeze_json(body))
            if edits
            else record
        )
        if edits or blocked is not None:
            audit.append(
                _entry(
                    line,
                    diagnosis,
                    record,
                    body,
                    applied=bool(edits),
                    blocked=blocked,
                    edits=tuple(edits),
                )
            )
        output.append(repaired)

    return RepairResult(tuple(output), tuple(audit))
