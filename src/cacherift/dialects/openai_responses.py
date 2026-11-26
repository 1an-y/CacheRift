"""OpenAI Responses API dialect adapter (full support)."""

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


def _extract_response_payload(record: CaptureRecord) -> Mapping[str, Any] | None:
    body = record.response_body
    if not isinstance(body, Mapping):
        return None
    events = body.get("events")
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes)):
        for event in reversed(events):
            if isinstance(event, Mapping) and event.get("type") == "response.completed":
                resp = event.get("response")
                if isinstance(resp, Mapping):
                    return resp
        return None
    return body


def _collect_responses_tools(
    body: Mapping[str, Any],
    wire_spans: Mapping[str, ByteSpan] | None,
    origin_map: Mapping[str, str],
) -> list[OrderedUnit]:
    tools = body.get("tools")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        return []
    units: list[OrderedUnit] = []
    seen_names: dict[str, int] = {}
    for idx, tool in enumerate(tools):
        name = tool.get("name") if isinstance(tool, Mapping) else None
        units.append(
            make_ordered_unit(
                unit_id=unique_tool_unit_id(name, idx, seen_names),
                json_path=f"$.tools[{idx}]",
                display_role=DisplayRole.TOOLS,
                projected_value=tool,
                wire_spans=wire_spans,
                origin_map=origin_map,
            )
        )
    return units


def _collect_responses_instructions(
    body: Mapping[str, Any],
    wire_spans: Mapping[str, ByteSpan] | None,
    origin_map: Mapping[str, str],
) -> list[OrderedUnit]:
    if "instructions" not in body or body["instructions"] is None:
        return []
    return [
        make_ordered_unit(
            unit_id="instructions",
            json_path="$.instructions",
            display_role=DisplayRole.SYSTEM_OR_INSTRUCTIONS,
            projected_value=body["instructions"],
            wire_spans=wire_spans,
            origin_map=origin_map,
        )
    ]


def _collect_responses_input(
    body: Mapping[str, Any],
    wire_spans: Mapping[str, ByteSpan] | None,
    origin_map: Mapping[str, str],
) -> list[OrderedUnit]:
    if "input" not in body or body["input"] is None:
        return []
    inp = body["input"]
    if isinstance(inp, Sequence) and not isinstance(inp, (str, bytes)):
        units: list[OrderedUnit] = []
        last_idx = len(inp) - 1
        for idx, item in enumerate(inp):
            role = item.get("role") or item.get("type") if isinstance(item, Mapping) else "item"
            display_role = (
                DisplayRole.CURRENT_INPUT if idx == last_idx else DisplayRole.HISTORY_MESSAGE
            )
            units.append(
                make_ordered_unit(
                    unit_id=f"input[{idx}]:{role}",
                    json_path=f"$.input[{idx}]",
                    display_role=display_role,
                    projected_value=item,
                    wire_spans=wire_spans,
                    origin_map=origin_map,
                )
            )
        return units
    return [
        make_ordered_unit(
            unit_id="input",
            json_path="$.input",
            display_role=DisplayRole.CURRENT_INPUT,
            projected_value=inp,
            wire_spans=wire_spans,
            origin_map=origin_map,
        )
    ]


class OpenAIResponsesAdapter:
    dialect_name = "openai_responses"

    def extract_ordered_units(
        self,
        record: CaptureRecord,
        capability: CapabilityInfo,
    ) -> tuple[tuple[OrderedUnit, ...], CacheConfigFingerprint]:
        body = record.parsed_body if isinstance(record.parsed_body, Mapping) else {}
        wire_spans = compute_wire_spans(record)

        section_order = capability.unit_order or ("tools", "instructions", "input")
        collectors = {
            "tools": _collect_responses_tools,
            "instructions": _collect_responses_instructions,
            "input": _collect_responses_input,
        }
        units: list[OrderedUnit] = []
        for section in section_order:
            collector = collectors.get(section)
            if collector is not None:
                units.extend(collector(body, wire_spans, record.origin_map))

        text_cfg = body.get("text")
        fmt = text_cfg.get("format") if isinstance(text_cfg, Mapping) else None
        output_schema_sha256 = sha256_hex(canonical_project_bytes(fmt)) if fmt is not None else None

        cache_params: list[tuple[str, str, Any]] = []
        for field_name in ("prompt_cache_key", "prompt_cache_retention"):
            if field_name in body and body[field_name] is not None:
                cache_params.append((f"$.{field_name}", field_name, freeze_json(body[field_name])))

        opts = body.get("prompt_cache_options")
        if isinstance(opts, Mapping):
            for key, val in opts.items():
                if key == "comparison_response_id":
                    continue
                cache_params.append(
                    (
                        f"$.prompt_cache_options.{key}",
                        f"prompt_cache_options.{key}",
                        freeze_json(val),
                    )
                )

        fingerprint = CacheConfigFingerprint(
            model=str(body.get("model", record.identity.model)),
            reasoning_or_thinking=freeze_json(body["reasoning"]) if "reasoning" in body else None,
            output_schema_sha256=output_schema_sha256,
            service_tier=str(body["service_tier"])
            if body.get("service_tier") is not None
            else None,
            cache_params=tuple(cache_params),
        )
        return tuple(units), fingerprint

    def extract_usage(self, record: CaptureRecord) -> UsageObservation:
        if not record.response_complete:
            return missing_usage(record, "response_incomplete_or_interrupted")

        resp = _extract_response_payload(record)
        if resp is None:
            return missing_usage(record, "missing_response_body")

        usage = resp.get("usage")
        if not isinstance(usage, Mapping):
            return missing_usage(record, "missing_usage")

        total_input = usage.get("input_tokens")
        output = usage.get("output_tokens")
        details = usage.get("input_tokens_details")
        if total_input is None or output is None:
            return incomplete_usage(usage, "missing_input_or_output_tokens")
        if not isinstance(details, Mapping) or details.get("cached_tokens") is None:
            return incomplete_usage(usage, "missing_input_tokens_details_cached_tokens")

        cached_tokens = int(details["cached_tokens"])
        total_input_int = int(total_input)
        output_int = int(output)
        if cached_tokens > total_input_int:
            return invalid_usage(usage, "cached_tokens_exceeds_input_tokens")

        writes: list[CacheWriteBucket] = []
        write_tokens = 0
        if details.get("cache_write_tokens") is not None:
            wt = int(details["cache_write_tokens"])
            if wt < 0:
                return invalid_usage(usage, "negative_cache_write_tokens")
            if wt > 0:
                writes.append(CacheWriteBucket(tier="default", tokens=wt))
                write_tokens += wt

        uncached_input = total_input_int - cached_tokens - write_tokens
        return finalize_usage_buckets(
            uncached_input=uncached_input,
            cache_read=cached_tokens,
            cache_writes=tuple(writes),
            output=output_int,
            raw_fields=usage,
        )

    def extract_provider_diagnostics(
        self,
        record: CaptureRecord,
    ) -> ProviderDiagnostics | None:
        resp = _extract_response_payload(record)
        if resp is None:
            return None
        diag = resp.get("prompt_cache_diagnostics")
        if not isinstance(diag, Mapping):
            return None

        divergences: list[str] = []
        raw_divs = diag.get("divergences")
        if isinstance(raw_divs, Sequence) and not isinstance(raw_divs, (str, bytes)):
            divergences.extend(str(d) for d in raw_divs)
        reason = diag.get("reason")
        if isinstance(reason, str) and reason and reason not in divergences:
            divergences.append(reason)

        compared_id = diag.get("compared_response_id")
        return ProviderDiagnostics(
            source="openai_prompt_cache_diagnostics",
            compared_response_id=str(compared_id) if compared_id is not None else None,
            reported_divergences=tuple(divergences),
            raw_payload=freeze_json(dict(diag)),
        )

    def extract_previous_response_ref(self, record: CaptureRecord) -> str | None:
        body = record.parsed_body
        if not isinstance(body, Mapping):
            return None
        opts = body.get("prompt_cache_options")
        if isinstance(opts, Mapping) and isinstance(opts.get("comparison_response_id"), str):
            return str(opts["comparison_response_id"])
        prev = body.get("previous_response_id")
        return str(prev) if isinstance(prev, str) and prev else None
