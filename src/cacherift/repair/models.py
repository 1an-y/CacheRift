"""Audit trail for audited repairs. Every hash carries the coordinate space it was taken in."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cacherift.core.models import CaptureRecord

WIRE_RAW = "wire_raw"
PROJECTED = "projected"


@dataclass(frozen=True, slots=True)
class RepairEdit:
    """One node rewritten by one rule. Hashes are over projected bytes; None means the node
    did not exist on that side (added or removed)."""

    rule: str
    json_path: str
    detail: str
    before_sha256: str | None
    after_sha256: str | None
    before_len: int | None
    after_len: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "json_path": self.json_path,
            "detail": self.detail,
            "space": PROJECTED,
            "sha256": {"before": self.before_sha256, "after": self.after_sha256},
            "len": {"before": self.before_len, "after": self.after_len},
        }


@dataclass(frozen=True, slots=True)
class RepairAuditEntry:
    """Why one record was rewritten, or why a rewrite that looked possible was refused."""

    record_id: str
    line: int
    lineage_status: str
    parent_record_id: str | None
    support_level: str
    applied: bool
    blocked_reason: str | None
    edits: tuple[RepairEdit, ...]
    before_space: str = PROJECTED
    before_sha256: str | None = None
    before_len: int | None = None
    after_sha256: str | None = None
    after_len: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "line": self.line,
            "lineage_status": self.lineage_status,
            "parent_record_id": self.parent_record_id,
            "support_level": self.support_level,
            "applied": self.applied,
            "blocked_reason": self.blocked_reason,
            "record_sha256": {
                "before": self.before_sha256,
                "before_space": self.before_space,
                "before_len": self.before_len,
                "after": self.after_sha256,
                "after_space": PROJECTED if self.after_sha256 is not None else None,
                "after_len": self.after_len,
            },
            "edits": [edit.to_json() for edit in self.edits],
        }


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Repaired records in capture order plus the audit trail. Nothing here has been written."""

    records: tuple[CaptureRecord, ...]
    audit: tuple[RepairAuditEntry, ...]

    @property
    def summary(self) -> dict[str, int]:
        return {
            "records": len(self.records),
            "repaired": sum(1 for entry in self.audit if entry.applied),
            "blocked": sum(1 for entry in self.audit if not entry.applied),
        }
