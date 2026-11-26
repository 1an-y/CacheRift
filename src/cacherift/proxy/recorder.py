"""Append-only JSONL capture writer that is safe to share across concurrent requests."""

from __future__ import annotations

import asyncio
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cacherift.core.models import CaptureRecord, TurnDiagnosis
from cacherift.io.capture import dump_capture_record

if TYPE_CHECKING:
    from cacherift.cli import DiagnosisSession


def live_line(diagnosis: TurnDiagnosis) -> str:
    """One line per captured request: numbers and codes only, never prompt text."""
    usage = diagnosis.usage
    if usage.buckets is not None:
        buckets = usage.buckets
        hit = (
            f"read={buckets.cache_read}/{buckets.total_input} "
            f"({buckets.reported_hit_ratio * 100:.1f}%)"
        )
        if buckets.cache_writes:
            hit += " writes=" + "+".join(f"{w.tier}:{w.tokens}" for w in buckets.cache_writes)
    else:
        hit = f"usage={usage.state.value}"
    changes = ",".join(sorted({c.change_kind.value for c in diagnosis.observed_changes}))
    lineage = diagnosis.lineage.status.value
    if diagnosis.lineage.parent_record_id:
        lineage += f"({diagnosis.lineage.match_basis})"
    elif diagnosis.lineage.candidate_parent_ids:
        lineage += f"({len(diagnosis.lineage.candidate_parent_ids)} candidates)"
    causes = ",".join(cause.code for cause in diagnosis.candidate_causes)
    return (
        f"{diagnosis.record_id} {diagnosis.identity.dialect} model={diagnosis.identity.model} "
        f"{hit} changes={changes or 'none'} lineage={lineage} causes={causes or '-'}"
    )


def _complain(record_id: str, action: str, exc: Exception) -> None:
    # The exception type only: messages can embed paths or response text.
    print(f"cacherift proxy: cannot {action} {record_id} ({type(exc).__name__})", file=sys.stderr)


class CaptureRecorder:
    """Writes one JSONL line per capture; concurrent writers are serialized, nothing else is."""

    def __init__(
        self,
        path: Path,
        *,
        live: bool = False,
        session: DiagnosisSession | None = None,
    ) -> None:
        self._fh = path.open("a", encoding="utf-8")
        self._lock = asyncio.Lock()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._prefix = f"cap-{stamp}-{secrets.token_hex(3)}"
        self._count = 0
        self._live = live
        self._session = session

    @property
    def closed(self) -> bool:
        return self._fh.closed

    def next_record_id(self) -> str:
        self._count += 1
        return f"{self._prefix}-{self._count:05d}"

    async def write(self, record: CaptureRecord) -> None:
        """Append one capture. Capturing is an observer: nothing here may raise into the proxy."""
        try:
            line = dump_capture_record(record) + "\n"
            async with self._lock:  # only the file write is serialized here, never network I/O
                self._fh.write(line)
                self._fh.flush()
        except Exception as exc:
            _complain(record.record_id, "capture", exc)
            return
        if self._live and self._session is not None:
            try:
                diagnosis = self._session.feed(record)
            except Exception as exc:
                _complain(record.record_id, "diagnose", exc)
                return
            print(live_line(diagnosis), file=sys.stderr, flush=True)

    def close(self) -> None:
        self._fh.close()
