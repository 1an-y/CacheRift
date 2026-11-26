"""DialectAdapter protocol and shared helper builders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from cacherift.core.freeze import canonical_project_bytes, freeze_json, sha256_hex
from cacherift.core.models import (
    ByteSpan,
    CacheConfigFingerprint,
    CacheWriteBucket,
    CapabilityInfo,
    CaptureRecord,
    DisplayRole,
    OffsetSpace,
    OrderedUnit,
    ProviderDiagnostics,
    TokenBuckets,
    UsageObservation,
    UsageState,
)
from cacherift.core.span import scan_json_spans


class DialectAdapter(Protocol):
    dialect_name: str

    def extract_ordered_units(
        self,
        record: CaptureRecord,
        capability: CapabilityInfo,
    ) -> tuple[tuple[OrderedUnit, ...], CacheConfigFingerprint]: ...

    def extract_usage(self, record: CaptureRecord) -> UsageObservation: ...

    def extract_provider_diagnostics(
        self,
        record: CaptureRecord,
    ) -> ProviderDiagnostics | None: ...

    def extract_previous_response_ref(self, record: CaptureRecord) -> str | None: ...


def strip_cache_control(value: Any, json_path: str) -> tuple[Any, list[tuple[str, Any]]]:
    """Copy a JSON structure without any ``cache_control`` key.

    Returns the copy and, for every key removed, its JSON path and its value.
    """
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        removed: list[tuple[str, Any]] = []
        for key, item in value.items():
            child_path = f"{json_path}.{key}"
            if key == "cache_control":
                removed.append((child_path, item))
                continue
            cleaned[str(key)], found = strip_cache_control(item, child_path)
            removed.extend(found)
        return cleaned, removed
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        removed = []
        for index, item in enumerate(value):
            copied, found = strip_cache_control(item, f"{json_path}[{index}]")
            items.append(copied)
            removed.extend(found)
        return items, removed
    return value, []


def compute_wire_spans(record: CaptureRecord) -> dict[str, ByteSpan] | None:
    """Return WIRE_RAW spans only when captured_body_bytes exist."""
    if record.captured_body_bytes is None:
        return None
    return scan_json_spans(record.captured_body_bytes, space=OffsetSpace.WIRE_RAW)


def make_ordered_unit(
    *,
    unit_id: str,
    json_path: str,
    display_role: DisplayRole,
    projected_value: Any,
    wire_spans: Mapping[str, ByteSpan] | None,
    origin_map: Mapping[str, str],
) -> OrderedUnit:
    """Construct an OrderedUnit with canonical order-preserving bytes and optional wire span."""
    projected_bytes = canonical_project_bytes(projected_value)
    wire_span = wire_spans.get(json_path) if wire_spans is not None else None
    return OrderedUnit(
        unit_id=unit_id,
        json_path=json_path,
        display_role=display_role,
        projected_bytes=projected_bytes,
        projected_sha256=sha256_hex(projected_bytes),
        wire_span=wire_span,
        estimated_tokens=max(1, len(projected_bytes) // 4) if projected_bytes else 0,
        origin=origin_map.get(json_path),
    )


def unique_tool_unit_id(raw_name: Any, index: int, seen: dict[str, int]) -> str:
    """Return a deterministic logical ID for a tool while disambiguating duplicate names."""
    if isinstance(raw_name, str) and raw_name:
        count = seen.get(raw_name, 0) + 1
        seen[raw_name] = count
        if count == 1:
            return f"tool:{raw_name}"
        return f"tool:{raw_name}#{count}"
    return f"tool[{index}]"


def finalize_usage_buckets(
    *,
    uncached_input: int,
    cache_read: int,
    cache_writes: tuple[CacheWriteBucket, ...],
    output: int,
    raw_fields: Mapping[str, Any],
    notes: tuple[str, ...] = (),
) -> UsageObservation:
    """Validate non-overlapping token buckets and compute hit ratio without division by zero."""
    frozen_raw = freeze_json(dict(raw_fields))
    if (
        uncached_input < 0
        or cache_read < 0
        or output < 0
        or any(w.tokens < 0 for w in cache_writes)
    ):
        return UsageObservation(
            state=UsageState.INVALID,
            buckets=None,
            raw_fields=frozen_raw,
            notes=notes + ("negative_token_count",),
        )

    total_writes = sum(w.tokens for w in cache_writes)
    total_input = uncached_input + cache_read + total_writes
    if total_input <= 0 or cache_read > total_input:
        return UsageObservation(
            state=UsageState.INVALID,
            buckets=None,
            raw_fields=frozen_raw,
            notes=notes + ("inconsistent_total_input",),
        )

    buckets = TokenBuckets(
        uncached_input=uncached_input,
        cache_read=cache_read,
        cache_writes=cache_writes,
        output=output,
        total_input=total_input,
        reported_hit_ratio=cache_read / total_input,
    )
    return UsageObservation(
        state=UsageState.COMPLETE,
        buckets=buckets,
        raw_fields=frozen_raw,
        notes=notes,
    )


def _stream_events(record: CaptureRecord) -> Sequence[Any] | None:
    """The captured SSE events when the response was captured as a stream, else None."""
    body = record.response_body
    events = body.get("events") if isinstance(body, Mapping) else None
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes)):
        return events
    return None


def is_streamed(record: CaptureRecord) -> bool:
    """True when the response was captured as a list of SSE events rather than one JSON body."""
    return _stream_events(record) is not None


def find_usage(record: CaptureRecord, key: str, *aliases: str) -> Mapping[str, Any] | None:
    """The usage object of a response, streamed or not; None when the response has none.

    A stream repeats cumulative snapshots, so only the last frame carrying an object under
    ``key`` (or one of ``aliases``) counts: earlier frames must never be added up. Frames whose
    value is null or not an object, and non-JSON frames such as the ``[DONE]`` sentinel, are
    skipped.
    """
    body = record.response_body
    if not isinstance(body, Mapping):
        return None
    events = _stream_events(record)
    frames: Iterable[Mapping[str, Any]] = (
        [event for event in reversed(events) if isinstance(event, Mapping)]
        if events is not None
        else [body]
    )
    for frame in frames:
        for name in (key, *aliases):
            value = frame.get(name)
            if isinstance(value, Mapping):
                return value
    return None


def usage_missing(record: CaptureRecord, note: str) -> UsageObservation:
    """MISSING for a response that carries no usage object.

    A stream says so in its own words: the usual cause there is a client that never asked for
    usage frames, which is a different fact from a response body that left the usage out.
    """
    if is_streamed(record):
        return missing_usage(record, "missing_usage_in_stream")
    return missing_usage(record, note)


def missing_usage(record: CaptureRecord, note: str) -> UsageObservation:
    # Keep only the usage object. Reports are printed without --include-snippets, so the rest of
    # the response (model output, error text) must never ride along in raw_fields.
    body = record.response_body
    usage = body.get("usage") if isinstance(body, Mapping) else None
    raw = dict(usage) if isinstance(usage, Mapping) else {}
    return UsageObservation(
        state=UsageState.MISSING,
        buckets=None,
        raw_fields=freeze_json(raw),
        notes=(note,),
    )


def incomplete_usage(raw_usage: Mapping[str, Any], note: str) -> UsageObservation:
    return UsageObservation(
        state=UsageState.INCOMPLETE,
        buckets=None,
        raw_fields=freeze_json(dict(raw_usage)),
        notes=(note,),
    )


def invalid_usage(raw_usage: Mapping[str, Any], note: str) -> UsageObservation:
    return UsageObservation(
        state=UsageState.INVALID,
        buckets=None,
        raw_fields=freeze_json(dict(raw_usage)),
        notes=(note,),
    )


def get_dialect_adapter(dialect: str) -> DialectAdapter:
    """Return the DialectAdapter matching ``dialect`` (or UnknownDialectAdapter)."""
    from cacherift.dialects.anthropic_messages import AnthropicMessagesAdapter
    from cacherift.dialects.deepseek_chat import DeepSeekChatAdapter
    from cacherift.dialects.gemini_generate import GeminiGenerateAdapter
    from cacherift.dialects.openai_chat import OpenAIChatAdapter
    from cacherift.dialects.openai_responses import OpenAIResponsesAdapter

    adapters: dict[str, DialectAdapter] = {
        "openai_responses": OpenAIResponsesAdapter(),
        "anthropic_messages": AnthropicMessagesAdapter(),
        "openai_chat": OpenAIChatAdapter(),
        "deepseek_chat": DeepSeekChatAdapter(),
        "gemini_generate_content": GeminiGenerateAdapter(),
    }
    return adapters.get(dialect, UnknownDialectAdapter())


class UnknownDialectAdapter:
    dialect_name = "unknown"

    def extract_ordered_units(
        self,
        record: CaptureRecord,
        capability: CapabilityInfo,
    ) -> tuple[tuple[OrderedUnit, ...], CacheConfigFingerprint]:
        del capability
        body = record.parsed_body if isinstance(record.parsed_body, Mapping) else {}
        wire_spans = compute_wire_spans(record)
        unit = make_ordered_unit(
            unit_id="payload",
            json_path="$",
            display_role=DisplayRole.UNKNOWN,
            projected_value=body,
            wire_spans=wire_spans,
            origin_map=record.origin_map,
        )
        cfg = CacheConfigFingerprint(
            model=str(body.get("model", record.identity.model)),
            reasoning_or_thinking=None,
            output_schema_sha256=None,
            service_tier=None,
            cache_params=(),
        )
        return (unit,), cfg

    def extract_usage(self, record: CaptureRecord) -> UsageObservation:
        return missing_usage(record, "unknown_dialect")

    def extract_provider_diagnostics(
        self,
        record: CaptureRecord,
    ) -> ProviderDiagnostics | None:
        del record
        return None

    def extract_previous_response_ref(self, record: CaptureRecord) -> str | None:
        del record
        return None
