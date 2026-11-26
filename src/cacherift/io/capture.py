"""JSONL capture record serialization supporting raw wire bytes and parsed-only imports."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, cast

from cacherift.core.freeze import freeze_json, thaw_json
from cacherift.core.models import CaptureRecord, DialectName, EndpointIdentity

_VALID_DIALECTS: frozenset[str] = frozenset(
    {
        "openai_responses",
        "anthropic_messages",
        "openai_chat",
        "deepseek_chat",
        "gemini_generate_content",
        "unknown",
    }
)


def _parse_identity(raw_identity: dict[str, Any]) -> EndpointIdentity:
    dialect_raw = str(raw_identity.get("dialect", "unknown"))
    dialect: DialectName = cast(
        DialectName,
        dialect_raw if dialect_raw in _VALID_DIALECTS else "unknown",
    )
    return EndpointIdentity(
        dialect=dialect,
        provider=str(raw_identity.get("provider", "unknown")),
        model=str(raw_identity.get("model", "unknown")),
        endpoint_path=str(raw_identity.get("endpoint_path", "")),
        gateway_profile=raw_identity.get("gateway_profile"),
        pricing_table_id=raw_identity.get("pricing_table_id"),
    )


def _decode_raw_bytes(payload: dict[str, Any]) -> bytes | None:
    if "raw_body_b64" in payload and payload["raw_body_b64"] is not None:
        return base64.b64decode(str(payload["raw_body_b64"]).encode("ascii"))
    if "raw_body_utf8" in payload and payload["raw_body_utf8"] is not None:
        return str(payload["raw_body_utf8"]).encode("utf-8")
    return None


def capture_record_from_dict(payload: dict[str, Any]) -> CaptureRecord:
    """Construct an immutable CaptureRecord from a decoded JSONL dictionary."""
    raw_bytes = _decode_raw_bytes(payload)
    if "parsed_body" in payload and payload["parsed_body"] is not None:
        parsed_obj = payload["parsed_body"]
    elif raw_bytes is not None:
        parsed_obj = json.loads(raw_bytes.decode("utf-8"))
    else:
        rec_id = payload.get("record_id", "<unknown>")
        raise ValueError(
            f"Capture record {rec_id} lacks both raw_body and parsed_body"
        )

    headers_raw = {str(k): str(v) for k, v in dict(payload.get("request_headers") or {}).items()}
    origin_raw = {str(k): str(v) for k, v in dict(payload.get("origin_map") or {}).items()}
    response_body_raw = payload.get("response_body")

    return CaptureRecord(
        record_id=str(payload["record_id"]),
        timestamp=float(payload["timestamp"]),
        identity=_parse_identity(dict(payload["identity"])),
        request_headers=freeze_json(headers_raw),
        captured_body_bytes=raw_bytes,
        parsed_body=freeze_json(parsed_obj),
        response_id=payload.get("response_id"),
        response_body=freeze_json(response_body_raw) if response_body_raw is not None else None,
        response_complete=bool(payload.get("response_complete", True)),
        client_session_id=payload.get("client_session_id"),
        client_trace_id=payload.get("client_trace_id"),
        parent_record_id=payload.get("parent_record_id"),
        origin_map=freeze_json(origin_raw),
    )


def load_capture_records(path: Path) -> list[CaptureRecord]:
    """Read newline-delimited capture records from ``path``."""
    records: list[CaptureRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no} of {path}: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"Expected JSON object on line {line_no} of {path}")
            records.append(capture_record_from_dict(obj))
    return records


def dump_capture_record(record: CaptureRecord) -> str:
    """Serialize a CaptureRecord to a single JSONL line preserving wire-raw vs parsed-only."""
    data: dict[str, Any] = {
        "record_id": record.record_id,
        "timestamp": record.timestamp,
        "identity": {
            "dialect": record.identity.dialect,
            "provider": record.identity.provider,
            "model": record.identity.model,
            "endpoint_path": record.identity.endpoint_path,
            "gateway_profile": record.identity.gateway_profile,
            "pricing_table_id": record.identity.pricing_table_id,
        },
        "request_headers": thaw_json(record.request_headers),
        "response_id": record.response_id,
        "response_body": thaw_json(record.response_body)
        if record.response_body is not None
        else None,
        "response_complete": record.response_complete,
        "client_session_id": record.client_session_id,
        "client_trace_id": record.client_trace_id,
        "parent_record_id": record.parent_record_id,
        "origin_map": thaw_json(record.origin_map),
    }
    if record.captured_body_bytes is not None:
        try:
            data["raw_body_utf8"] = record.captured_body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            data["raw_body_b64"] = base64.b64encode(record.captured_body_bytes).decode("ascii")
    else:
        data["parsed_body"] = thaw_json(record.parsed_body)
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
