"""Anthropic Messages API dialect adapter (full support)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cacherift.core.freeze import canonical_project_bytes, freeze_json, sha256_hex
from cacherift.core.models import (
    ByteSpan,
    CacheConfigFingerprint,
    CacheWriteBucket,
    CapabilityInfo,
    CaptureRecord,
    DisplayRole,
    OrderedUnit,
    ProviderDiagnostics,
    UsageObservation,
)
from cacherift.dialects.base import (
    compute_wire_spans,
    finalize_usage_buckets,
    incomplete_usage,
    invalid_usage,
    make_ordered_unit,
    missing_usage,
    unique_tool_unit_id,
)


def _strip_cache_control(value: Any, json_path: str, found: list[tuple[str, str, Any]]) -> Any:
    """Copy frozen structure without nested cache_control, recording breakpoint locations."""
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            child_path = f"{json_path}.{key}"
            if key == "cache_control":
                found.append((child_path, "cache_control", freeze_json(item)))
                continue
            cleaned[str(key)] = _strip_cache_control(item, child_path, found)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [
            _strip_cache_control(item, f"{json_path}[{idx}]", found)
            for idx, item in enumerate(value)
        ]
    return value


def _extract_anthropic_usage_snapshot(record: CaptureRecord) -> Mapping[str, Any] | None:
    body = record.response_body
    if not isinstance(body, Mapping):
        return None
    events = body.get("events")
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes)):
        merged: dict[str, Any] = {}
        for event in events:
            if not isinstance(event, Mapping):
                continue
            etype = event.get("type")
            if etype == "message_start":
                msg = event.get("message")
                if isinstance(msg, Mapping) and isinstance(msg.get("usage"), Mapping):
                    merged.update(msg["usage"])
            elif etype == "message_delta" and isinstance(event.get("usage"), Mapping):
                # Anthropic message_delta.usage carries cumulative snapshots: overwrite, never sum.
                merged.update(event["usage"])
        return merged or None
    usage = body.get("usage")
    return usage if isinstance(usage, Mapping) else None


def _collect_anthropic_tools(
    body: Mapping[str, Any],
    wire_spans: Mapping[str, ByteSpan] | None,
    origin_map: Mapping[str, str],
    breakpoints: list[tuple[str, str, Any]],
) -> list[OrderedUnit]:
    tools = body.get("tools")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        return []
    units: list[OrderedUnit] = []
    seen: dict[str, int] = {}
    for idx, tool in enumerate(tools):
        path = f"$.tools[{idx}]"
        name = tool.get("name") if isinstance(tool, Mapping) else None
        cleaned = _strip_cache_control(tool, path, breakpoints)
        units.append(
            make_ordered_unit(
                unit_id=unique_tool_unit_id(name, idx, seen),
                json_path=path,
                display_role=DisplayRole.TOOLS,
                projected_value=cleaned,
                wire_spans=wire_spans,
                origin_map=origin_map,
            )
        )
    return units


def _collect_anthropic_system(
    body: Mapping[str, Any],
    wire_spans: Mapping[str, ByteSpan] | None,
    origin_map: Mapping[str, str],
    breakpoints: list[tuple[str, str, Any]],
) -> list[OrderedUnit]:
    if "system" not in body or body["system"] is None:
        return []
    system = body["system"]
    if isinstance(system, Sequence) and not isinstance(system, (str, bytes)):
        units: list[OrderedUnit] = []
        for idx, block in enumerate(system):
            path = f"$.system[{idx}]"
            cleaned = _strip_cache_control(block, path, breakpoints)
            units.append(
                make_ordered_unit(
                    unit_id=f"system[{idx}]",
                    json_path=path,
                    display_role=DisplayRole.SYSTEM_OR_INSTRUCTIONS,
                    projected_value=cleaned,
                    wire_spans=wire_spans,
                    origin_map=origin_map,
                )
            )
        return units
    cleaned_str = _strip_cache_control(system, "$.system", breakpoints)
    return [
        make_ordered_unit(
            unit_id="system",
            json_path="$.system",
            display_role=DisplayRole.SYSTEM_OR_INSTRUCTIONS,
            projected_value=cleaned_str,
            wire_spans=wire_spans,
            origin_map=origin_map,
        )
    ]


def _collect_anthropic_messages(
    body: Mapping[str, Any],
    wire_spans: Mapping[str, ByteSpan] | None,
    origin_map: Mapping[str, str],
    breakpoints: list[tuple[str, str, Any]],
) -> list[OrderedUnit]:
    messages = body.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return []
    units: list[OrderedUnit] = []
    last_idx = len(messages) - 1
    for idx, msg in enumerate(messages):
        path = f"$.messages[{idx}]"
        role = msg.get("role", "message") if isinstance(msg, Mapping) else "message"
        cleaned = _strip_cache_control(msg, path, breakpoints)
        display_role = DisplayRole.CURRENT_INPUT if idx == last_idx else DisplayRole.HISTORY_MESSAGE
        units.append(
            make_ordered_unit(
                unit_id=f"messages[{idx}]:{role}",
                json_path=path,
                display_role=display_role,
                projected_value=cleaned,
                wire_spans=wire_spans,
                origin_map=origin_map,
            )
        )
    return units


class AnthropicMessagesAdapter:
    dialect_name = "anthropic_messages"

    def extract_ordered_units(
        self,
        record: CaptureRecord,
        capability: CapabilityInfo,
    ) -> tuple[tuple[OrderedUnit, ...], CacheConfigFingerprint]:
        del capability
        body = record.parsed_body if isinstance(record.parsed_body, Mapping) else {}
        wire_spans = compute_wire_spans(record)

        breakpoints: list[tuple[str, str, Any]] = []
        if "cache_control" in body and body["cache_control"] is not None:
            breakpoints.append(
                ("$.cache_control", "root_cache_control", freeze_json(body["cache_control"]))
            )

        units: list[OrderedUnit] = []
        units.extend(_collect_anthropic_tools(body, wire_spans, record.origin_map, breakpoints))
        units.extend(_collect_anthropic_system(body, wire_spans, record.origin_map, breakpoints))
        units.extend(_collect_anthropic_messages(body, wire_spans, record.origin_map, breakpoints))

        out_cfg = body.get("output_config")
        fmt = out_cfg.get("format") if isinstance(out_cfg, Mapping) else body.get("output_format")
        output_schema_sha256 = sha256_hex(canonical_project_bytes(fmt)) if fmt is not None else None

        fingerprint = CacheConfigFingerprint(
            model=str(body.get("model", record.identity.model)),
            reasoning_or_thinking=freeze_json(body["thinking"]) if "thinking" in body else None,
            output_schema_sha256=output_schema_sha256,
            service_tier=str(body["service_tier"])
            if body.get("service_tier") is not None
            else None,
            cache_params=tuple(breakpoints),
        )
        return tuple(units), fingerprint

    def extract_usage(self, record: CaptureRecord) -> UsageObservation:
        if not record.response_complete:
            return missing_usage(record, "response_incomplete_or_interrupted")

        usage = _extract_anthropic_usage_snapshot(record)
        if usage is None:
            return missing_usage(record, "missing_usage")

        if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
            return incomplete_usage(usage, "missing_input_or_output_tokens")

        uncached_input = int(usage["input_tokens"])
        output = int(usage["output_tokens"])
        cache_read = int(usage.get("cache_read_input_tokens") or 0)

        tier_writes: list[CacheWriteBucket] = []
        cache_creation_obj = usage.get("cache_creation")
        raw_total_writes = usage.get("cache_creation_input_tokens")

        if isinstance(cache_creation_obj, Mapping):
            t5 = cache_creation_obj.get("ephemeral_5m_input_tokens")
            t1h = cache_creation_obj.get("ephemeral_1h_input_tokens")
            if t5 is not None and int(t5) > 0:
                tier_writes.append(CacheWriteBucket(tier="5m", tokens=int(t5)))
            if t1h is not None and int(t1h) > 0:
                tier_writes.append(CacheWriteBucket(tier="1h", tokens=int(t1h)))
            for extra_key, extra_val in cache_creation_obj.items():
                if (
                    extra_key not in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
                    and extra_val is not None
                    and int(extra_val) > 0
                ):
                    tier_writes.append(CacheWriteBucket(tier=str(extra_key), tokens=int(extra_val)))

            tier_sum = sum(w.tokens for w in tier_writes)
            if raw_total_writes is not None and int(raw_total_writes) != tier_sum:
                return invalid_usage(usage, "cache_creation_total_mismatch_with_tiers")
        elif raw_total_writes is not None and int(raw_total_writes) > 0:
            tier_writes.append(CacheWriteBucket(tier="default", tokens=int(raw_total_writes)))

        return finalize_usage_buckets(
            uncached_input=uncached_input,
            cache_read=cache_read,
            cache_writes=tuple(tier_writes),
            output=output,
            raw_fields=usage,
        )

    def extract_provider_diagnostics(
        self,
        record: CaptureRecord,
    ) -> ProviderDiagnostics | None:
        body = record.response_body
        if not isinstance(body, Mapping):
            return None
        diag = body.get("diagnostics")
        if not isinstance(diag, Mapping):
            return None

        divergences: list[str] = []
        raw_divs = diag.get("divergences")
        if isinstance(raw_divs, Sequence) and not isinstance(raw_divs, (str, bytes)):
            divergences.extend(str(d) for d in raw_divs)
        reason = diag.get("cache_miss_reason")
        if isinstance(reason, str) and reason and reason not in divergences:
            divergences.append(reason)

        prev_msg_id = diag.get("previous_message_id")
        return ProviderDiagnostics(
            source="anthropic_diagnostics",
            compared_response_id=str(prev_msg_id) if prev_msg_id is not None else None,
            reported_divergences=tuple(divergences),
            raw_payload=freeze_json(dict(diag)),
            caveat=(
                "Anthropic structural diagnostics compare request layout "
                "independently of cache hit outcome."
            ),
        )

    def extract_previous_response_ref(self, record: CaptureRecord) -> str | None:
        body = record.parsed_body
        if not isinstance(body, Mapping):
            return None
        diag = body.get("diagnostics")
        if isinstance(diag, Mapping) and isinstance(diag.get("previous_message_id"), str):
            return str(diag["previous_message_id"])
        return None
