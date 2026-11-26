"""OpenAI Chat Completions dialect adapter (partial usage_only support)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cacherift.core.freeze import canonical_project_bytes, freeze_json, sha256_hex
from cacherift.core.models import (
    CacheConfigFingerprint,
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


class OpenAIChatAdapter:
    dialect_name = "openai_chat"

    def extract_ordered_units(
        self,
        record: CaptureRecord,
        capability: CapabilityInfo,
    ) -> tuple[tuple[OrderedUnit, ...], CacheConfigFingerprint]:
        del capability
        body = record.parsed_body if isinstance(record.parsed_body, Mapping) else {}
        wire_spans = compute_wire_spans(record)
        units: list[OrderedUnit] = []
        seen_tools: dict[str, int] = {}

        tools = body.get("tools")
        if isinstance(tools, Sequence) and not isinstance(tools, (str, bytes)):
            for idx, tool in enumerate(tools):
                fn = tool.get("function") if isinstance(tool, Mapping) else None
                name = fn.get("name") if isinstance(fn, Mapping) else None
                units.append(
                    make_ordered_unit(
                        unit_id=unique_tool_unit_id(name, idx, seen_tools),
                        json_path=f"$.tools[{idx}]",
                        display_role=DisplayRole.TOOLS,
                        projected_value=tool,
                        wire_spans=wire_spans,
                        origin_map=record.origin_map,
                    )
                )

        messages = body.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            last_idx = len(messages) - 1
            for idx, msg in enumerate(messages):
                role = msg.get("role", "message") if isinstance(msg, Mapping) else "message"
                if role in ("system", "developer"):
                    display_role = DisplayRole.SYSTEM_OR_INSTRUCTIONS
                elif idx == last_idx:
                    display_role = DisplayRole.CURRENT_INPUT
                else:
                    display_role = DisplayRole.HISTORY_MESSAGE
                units.append(
                    make_ordered_unit(
                        unit_id=f"messages[{idx}]:{role}",
                        json_path=f"$.messages[{idx}]",
                        display_role=display_role,
                        projected_value=msg,
                        wire_spans=wire_spans,
                        origin_map=record.origin_map,
                    )
                )

        resp_fmt = body.get("response_format")
        cache_params: list[tuple[str, str, Any]] = []
        if "prompt_cache_key" in body and body["prompt_cache_key"] is not None:
            cache_params.append(
                ("$.prompt_cache_key", "prompt_cache_key", freeze_json(body["prompt_cache_key"]))
            )

        cfg = CacheConfigFingerprint(
            model=str(body.get("model", record.identity.model)),
            reasoning_or_thinking=freeze_json(body["reasoning_effort"])
            if "reasoning_effort" in body
            else None,
            output_schema_sha256=sha256_hex(canonical_project_bytes(resp_fmt))
            if resp_fmt is not None
            else None,
            service_tier=str(body["service_tier"])
            if body.get("service_tier") is not None
            else None,
            cache_params=tuple(cache_params),
        )
        return tuple(units), cfg

    def extract_usage(self, record: CaptureRecord) -> UsageObservation:
        if not record.response_complete:
            return missing_usage(record, "response_incomplete_or_interrupted")
        body = record.response_body
        if not isinstance(body, Mapping) or not isinstance(body.get("usage"), Mapping):
            return missing_usage(record, "missing_usage")

        usage = body["usage"]
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details")
        if prompt_tokens is None or completion_tokens is None:
            return incomplete_usage(usage, "missing_prompt_or_completion_tokens")
        if not isinstance(details, Mapping) or details.get("cached_tokens") is None:
            return incomplete_usage(usage, "missing_prompt_tokens_details_cached_tokens")

        cached = int(details["cached_tokens"])
        total_prompt = int(prompt_tokens)
        if cached > total_prompt:
            return invalid_usage(usage, "cached_tokens_exceeds_prompt_tokens")

        return finalize_usage_buckets(
            uncached_input=total_prompt - cached,
            cache_read=cached,
            cache_writes=(),
            output=int(completion_tokens),
            raw_fields=usage,
        )

    def extract_provider_diagnostics(self, record: CaptureRecord) -> ProviderDiagnostics | None:
        del record
        return None

    def extract_previous_response_ref(self, record: CaptureRecord) -> str | None:
        del record
        return None
